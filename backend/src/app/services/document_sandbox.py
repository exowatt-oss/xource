"""Fail-closed, rootless Linux namespaces for native document parsing.

The web process hands a byte-only stdin to a secret-free, separate PID/user/net
namespace. No app source, home, env file, socket or credential is mounted. Only
installed Python/parser runtime files are bound read-only. Seccomp prohibits new
processes and namespace/ptrace/kernel-key escapes. No Docker socket, sudo, or
host daemon authority is granted to the application.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import importlib.util
import os
import re
import subprocess
import sys
import sysconfig
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

BOOTSTRAP = (
    "import sys,runpy;sys.path.insert(0,'/packages');"
    "sys.argv=['/work/worker.py',*sys.argv[1:]];"
    "runpy.run_path('/work/worker.py',run_name='__main__')"
)
DENIED_SYSCALLS = (
    "clone",
    "clone3",
    "fork",
    "vfork",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "bpf",
    "perf_event_open",
    "keyctl",
    "add_key",
    "request_key",
    "mount",
    "umount2",
    "pivot_root",
    "open_by_handle_at",
    "setns",
    "unshare",
    "userfaultfd",
    "io_uring_setup",
)


class SandboxUnavailable(OSError):
    """No safe parser execution path; never fall back silently."""


@lru_cache(maxsize=1)
def _runtime_binds() -> tuple[tuple[str, str], ...]:
    """Resolve only trusted installed runtime dependencies, never uploaded paths."""
    python = Path(sys.executable).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    binds = {(str(python), str(python))}
    # A base interpreter may put site-packages *inside* stdlib. Do not expose
    # arbitrary installed applications/packages by binding that directory whole.
    for child in stdlib.iterdir():
        if child.name not in {"site-packages", "dist-packages"}:
            binds.add((str(child.resolve()), str(child)))
    libraries = {python, *stdlib.joinpath("lib-dynload").glob("*.so")}
    for name in ("defusedxml", "PIL", "pypdfium2", "pypdfium2_raw", "pypdfium2_cfg"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise SandboxUnavailable("Parser dependency missing")
        source = Path(spec.origin).parent.resolve()
        binds.add((str(source), f"/packages/{name}"))
        libraries.update(source.glob("*.so*"))
        if name == "PIL":
            # Pillow wheels resolve native libraries through a relative RPATH.
            for directory in source.parent.glob("pillow.libs"):
                binds.add((str(directory.resolve()), "/packages/pillow.libs"))
                libraries.update(directory.glob("*.so*"))
    deadline = time.monotonic() + 10
    for library in sorted(libraries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxUnavailable("Parser runtime setup timed out")
        try:
            completed = subprocess.run(  # noqa: S603 - installed ELF, fixed ldd, no shell
                ["/usr/bin/ldd", str(library)],
                capture_output=True,
                text=True,
                timeout=min(5, remaining),
                env={"PATH": os.defpath, "LC_ALL": "C"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailable("Parser runtime setup timed out") from exc
        if completed.returncode != 0:
            raise SandboxUnavailable("Could not resolve parser runtime")
        for dependency in re.findall(r"/[^\s()]+", completed.stdout):
            path = Path(dependency)
            if not path.is_file():
                raise SandboxUnavailable("Parser runtime library missing")
            binds.add((str(path.resolve()), str(path)))
    return tuple(sorted(binds))


@contextmanager
def _seccomp_fd():
    library = ctypes.util.find_library("seccomp")
    if library is None:
        raise SandboxUnavailable("libseccomp missing")
    seccomp = ctypes.CDLL(library)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    resolver = getattr(seccomp, "seccomp_syscall_resolve_name_arch", None)
    if resolver is None:
        raise SandboxUnavailable("Required seccomp resolver unavailable")
    resolver.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
    resolver.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = seccomp.seccomp_init(0x7FFF0000)  # allow default; deny high-risk classes below
    if not context:
        raise SandboxUnavailable("Seccomp initialization failed")
    fd = -1
    try:
        for name in DENIED_SYSCALLS:
            # SCMP_ARCH_NATIVE=0; -1 is unknown, while values below -1 are
            # recognized pseudo-syscalls absent on this architecture (e.g. fork
            # on aarch64). Never silently omit a name unknown to old libseccomp.
            number = resolver(0, name.encode("ascii"))
            if number == -1:
                raise SandboxUnavailable("Required seccomp syscall is unknown")
            if number >= 0 and seccomp.seccomp_rule_add(
                context, 0x00050000 | errno.EPERM, number, 0
            ):
                raise SandboxUnavailable("Seccomp rule failed")
        fd = os.memfd_create("xource-parser-seccomp", os.MFD_CLOEXEC)
        if seccomp.seccomp_export_bpf(context, fd):
            raise SandboxUnavailable("Seccomp export failed")
        os.lseek(fd, 0, os.SEEK_SET)
        yield fd
    finally:
        if fd >= 0:
            os.close(fd)
        seccomp.seccomp_release(context)


@contextmanager
def command(worker: Path, operation: str, kind: str, *, mode="required", is_prod=True):
    """Return fixed argv and the sole extra inheritable FD (a seccomp policy)."""
    if mode == "local-unsafe" and not is_prod:
        # Explicit test/developer-only escape hatch. Never an automatic fallback.
        yield [sys.executable, "-I", "-B", str(worker), operation, kind], ()
        return
    if mode != "required" or sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file():
        raise SandboxUnavailable("Linux bubblewrap is required")
    with _seccomp_fd() as fd:
        argv = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/tmp",
            "--chdir",
            "/tmp",
            "--seccomp",
            str(fd),
        ]
        for source, target in _runtime_binds():
            argv.extend(("--ro-bind", source, target))
        argv.extend(("--ro-bind", str(worker.resolve()), "/work/worker.py"))
        # Parsers consume stdin and emit stdout; no writable disk/scratch is
        # necessary. Prevent even directory/inode exhaustion on sandbox tmpfs.
        argv.extend(("--remount-ro", "/", "--remount-ro", "/dev", "--remount-ro", "/proc"))
        argv.extend(
            (
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-B",
                "-c",
                BOOTSTRAP,
                operation,
                kind,
            )
        )
        yield argv, (fd,)
