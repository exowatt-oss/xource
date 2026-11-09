"""Observe the actual service confinement environment without blocking startup.

The existing full gate handles trusted fixtures only. A successful observation
never enables processing; operator configuration and the deployment gate remain
independent requirements. Public liveness/readiness need no native parser.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import structlog

log = structlog.get_logger("document_runtime")
PROBE_TIMEOUT_S = 90
_GATE = Path(__file__).resolve().parents[4] / "scripts/check-document-sandbox.py"
_status = "checking"


def sandbox_status() -> str:
    return _status


async def probe_sandbox() -> None:
    """Run the strict gate asynchronously, with bounded lifetime and clean env."""
    global _status
    _status = "checking"
    process = None
    spawn = None
    try:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                str(_GATE),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": os.defpath, "PYTHONUTF8": "1"},
                start_new_session=True,
            )
        )
        # Cancellation while spawning must still acquire the owned process so
        # cleanup can kill/reap it. Never leave an unobserved creation task.
        process = await asyncio.shield(spawn)
        await asyncio.wait_for(process.wait(), timeout=PROBE_TIMEOUT_S)
        _status = "ready" if process.returncode == 0 else "unavailable"
    except asyncio.CancelledError:
        _status = "unavailable"
        raise
    except (OSError, TimeoutError):
        _status = "unavailable"
    finally:
        if process is None and spawn is not None:
            try:
                process = await spawn
            except OSError:
                pass
        if process is not None:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        # Diagnostics stay out of API responses and never contain command
        # output, host paths, credentials or parser stderr.
        log.info("document_sandbox_probe", status=_status, uid=os.getuid())
