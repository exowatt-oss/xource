"""Subprocess-only parsers for untrusted file bytes.

The child applies CPU/address-space (or macOS data)/file-size limits before
parser imports. The parent enforces the wall clock, output size, and process-
group cleanup. These are resource bounds, NOT an OS/filesystem/network sandbox.
No Office automation, macros, external relationships, network fetching, or
archive extraction. Parser imports stay inside the child, not the web process.
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import PurePosixPath

MAX_INPUT = 25 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_TEXT = 250_000
MAX_PARAGRAPHS = 300
MAX_SHEETS = 12
MAX_ROWS = 200
MAX_COLUMNS = 24
MAX_CELLS = 2000
MAX_SLIDES = 80
MAX_PDF_PAGES = 100
MAX_THUMB_SIDE = 512

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class UnsafeDocument(Exception):
    """Known bad or structurally oversized file, with no untrusted message exposed."""


class TextCollector:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def add(self, raw: str) -> str:
        remaining = MAX_TEXT - self.size
        value = raw.strip()[: max(0, remaining)]
        if len(raw.strip()) > len(value):
            self.truncated = True
        if value:
            self.parts.append(value)
            self.size += len(value) + 1
        return value

    @property
    def text(self) -> str:
        return "\n".join(self.parts)[:MAX_TEXT]


def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise UnsafeDocument
        total = 0
        seen: set[str] = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                not entry.filename
                or entry.filename.startswith("/")
                or "\\" in entry.filename
                or ".." in path.parts
                or ":" in entry.filename
                or entry.flag_bits & 0x1
                or entry.filename in seen
            ):
                raise UnsafeDocument
            seen.add(entry.filename)
            if entry.is_dir():
                continue
            total += entry.file_size
            if (
                entry.file_size > MAX_ENTRY_BYTES
                or total > MAX_ZIP_BYTES
                or entry.file_size > max(entry.compress_size, 1) * 200
            ):
                raise UnsafeDocument
        return archive
    except (zipfile.BadZipFile, ValueError, OverflowError) as exc:
        raise UnsafeDocument from exc


def _xml(archive: zipfile.ZipFile, name: str):
    from defusedxml import ElementTree as ET

    info = archive.getinfo(name)
    if info.file_size > MAX_ENTRY_BYTES:
        raise UnsafeDocument
    with archive.open(info) as stream:
        data = stream.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise UnsafeDocument
    return ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def _paragraph(element, ns: str) -> str:
    # w:tab/w:br matter in plain text, while attributes, field instructions,
    # comments, and embedded objects must never be interpreted or executed.
    parts = []
    for node in element.iter():
        if node.tag in {f"{ns}t", f"{ns}v"} and node.text:
            parts.append(node.text)
        elif node.tag == f"{ns}tab":
            parts.append("\t")
        elif node.tag in {f"{ns}br", f"{ns}cr"}:
            parts.append("\n")
    return "".join(parts)


def _docx(archive: zipfile.ZipFile) -> dict:
    root = _xml(archive, "word/document.xml")
    collector = TextCollector()
    paragraphs: list[str] = []
    for index, paragraph in enumerate(root.findall(f".//{W}body//{W}p")):
        if index >= MAX_PARAGRAPHS:
            collector.truncated = True
            break
        raw = _paragraph(paragraph, W)[:2000]
        value = collector.add(raw)
        if value:
            paragraphs.append(value)
    return {
        "kind": "docx",
        "paragraphs": paragraphs,
        "text": collector.text,
        "truncated": collector.truncated,
    }


def _xlsx(archive: zipfile.ZipFile) -> dict:
    workbook = _xml(archive, "xl/workbook.xml")
    rels = _xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {}
    for rel in rels.findall(f"{REL}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        # OpenXML targets are relative to xl/workbook.xml; only worksheet
        # parts from the archive itself are eligible (never URL or traversal).
        target = target.lstrip("/")
        if target.startswith("xl/"):
            target = target[3:]
        if re.fullmatch(r"worksheets/[^/]+\.xml", target):
            targets[rel.get("Id")] = "xl/" + target
    shared: list[str] = []
    collector = TextCollector()
    if "xl/sharedStrings.xml" in archive.namelist():
        strings = _xml(archive, "xl/sharedStrings.xml")
        items = strings.findall(f"{S}si")
        if len(items) > 10000:
            collector.truncated = True
        for item in items[:10000]:
            shared.append("".join(t.text or "" for t in item.findall(f".//{S}t"))[:2000])
    sheets = []
    cells_total = 0
    listed = workbook.findall(f".//{S}sheets/{S}sheet")
    if len(listed) > MAX_SHEETS:
        collector.truncated = True
    for sheet in listed[:MAX_SHEETS]:
        target = targets.get(sheet.get(f"{R}id"))
        if not target or target not in archive.namelist():
            continue
        root = _xml(archive, target)
        name = sheet.get("name", "Sheet")[:120]
        collector.add(name)
        rows = []
        all_rows = root.findall(f".//{S}sheetData/{S}row")
        if len(all_rows) > MAX_ROWS:
            collector.truncated = True
        for row in all_rows[:MAX_ROWS]:
            row_cells = []
            for cell in row.findall(f"{S}c"):
                ref = cell.get("r", "")
                match = re.fullmatch(r"([A-Z]{1,3})[1-9][0-9]*", ref)
                if not match:
                    continue
                column = 0
                for char in match.group(1):
                    column = column * 26 + ord(char) - 64
                if column > MAX_COLUMNS:
                    collector.truncated = True
                    continue
                if cells_total >= MAX_CELLS:
                    collector.truncated = True
                    break
                kind = cell.get("t")
                value = cell.findtext(f"{S}v") or ""
                if kind == "s":
                    try:
                        value = shared[int(value)]
                    except (IndexError, ValueError):
                        value = ""
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall(f".//{S}is//{S}t"))
                # Formula contents are never evaluated; cached values only.
                value = value[:2000]
                if value:
                    clipped = collector.add(value)
                    row_cells.append({"ref": ref, "value": clipped})
                    cells_total += 1
            if row_cells:
                rows.append({"number": int(row.get("r", "0") or "0"), "cells": row_cells})
            if cells_total >= MAX_CELLS or collector.size >= MAX_TEXT:
                collector.truncated = True
                break
        sheets.append({"name": name, "rows": rows})
        if cells_total >= MAX_CELLS or collector.size >= MAX_TEXT:
            break
    return {
        "kind": "xlsx",
        "sheets": sheets,
        "text": collector.text,
        "truncated": collector.truncated,
    }


def _pptx(archive: zipfile.ZipFile) -> dict:
    names = []
    for name in archive.namelist():
        match = re.fullmatch(r"ppt/slides/slide([1-9][0-9]*)\.xml", name)
        if match:
            names.append((int(match.group(1)), name))
    names.sort()
    collector = TextCollector()
    slides = []
    if len(names) > MAX_SLIDES:
        collector.truncated = True
    for number, name in names[:MAX_SLIDES]:
        root = _xml(archive, name)
        paragraphs = []
        for index, paragraph in enumerate(root.findall(f".//{A}p")):
            if index >= MAX_PARAGRAPHS:
                collector.truncated = True
                break
            value = collector.add(_paragraph(paragraph, A)[:2000])
            if value:
                paragraphs.append(value)
        slides.append({"number": number, "paragraphs": paragraphs})
        if collector.size >= MAX_TEXT:
            collector.truncated = True
            break
    return {
        "kind": "pptx",
        "slides": slides,
        "text": collector.text,
        "truncated": collector.truncated,
    }


def _open_pdf(data: bytes):
    import pypdfium2 as pdfium
    from pypdfium2 import PdfiumError

    try:
        return pdfium.PdfDocument(data)
    except PdfiumError as exc:
        # Invalid/encrypted documents cannot be indexed without a password.
        raise UnsafeDocument from exc


def _pdf_text(data: bytes) -> dict:
    import ctypes

    from pypdfium2 import raw as pdfium_c

    collector = TextCollector()
    with _open_pdf(data) as pdf:
        if len(pdf) > MAX_PDF_PAGES:
            collector.truncated = True
        for index in range(min(len(pdf), MAX_PDF_PAGES)):
            remaining = MAX_TEXT - collector.size
            if remaining <= 0:
                collector.truncated = True
                break
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    available = textpage.count_chars()
                    if available > remaining:
                        collector.truncated = True
                    count = min(available, remaining)
                    if count:
                        # The high-level get_text_range() can size its native buffer
                        # from inserted text indices instead of the requested count.
                        # PDFium's raw API writes at most count + one terminator.
                        buffer = (ctypes.c_ushort * (count + 1))()
                        written = pdfium_c.FPDFText_GetText(textpage, 0, count, buffer)
                        if written < 0 or written > count + 1:
                            raise UnsafeDocument
                        if written:
                            raw = bytes(buffer)[: (written - 1) * 2]
                            collector.add(raw.decode("utf-16-le", errors="ignore"))
                finally:
                    textpage.close()
            finally:
                page.close()
            if collector.size >= MAX_TEXT:
                collector.truncated = True
                break
    return {"kind": "pdf", "text": collector.text, "truncated": collector.truncated}


def extract(data: bytes, kind: str) -> dict:
    if len(data) > MAX_INPUT:
        raise UnsafeDocument
    if kind in {"docx", "xlsx", "pptx"}:
        with _zip(data) as archive:
            return {
                "status": "ready",
                **{"docx": _docx, "xlsx": _xlsx, "pptx": _pptx}[kind](archive),
            }
    if kind == "pdf":
        return {"status": "ready", **_pdf_text(data)}
    if kind == "text":
        text = data.decode("utf-8", errors="replace")
        return {
            "status": "ready",
            "kind": "text",
            "text": text[:MAX_TEXT],
            "truncated": len(text) > MAX_TEXT,
        }
    raise UnsafeDocument


def thumbnail(data: bytes, kind: str) -> bytes:
    if len(data) > MAX_INPUT:
        raise UnsafeDocument
    if kind == "pdf":
        import math

        with _open_pdf(data) as pdf:
            if not len(pdf):
                raise UnsafeDocument
            page = pdf[0]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(side) and side > 0 for side in (width, height)):
                    raise UnsafeDocument
                scale = min(1.5, MAX_THUMB_SIDE / max(width, height))
                # PdfPage.render() uses ceil(side * scale); reject impossible or
                # oversized dimensions *before* it creates a native bitmap.
                pixels = (math.ceil(width * scale), math.ceil(height * scale))
                if min(pixels) < 1 or max(pixels) > MAX_THUMB_SIDE:
                    raise UnsafeDocument
                bitmap = page.render(
                    scale=scale, may_draw_forms=False, prefer_bgrx=True, rev_byteorder=True
                )
                try:
                    if (
                        min(bitmap.width, bitmap.height) < 1
                        or max(bitmap.width, bitmap.height) > MAX_THUMB_SIDE
                    ):
                        raise UnsafeDocument
                    with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                        output = io.BytesIO()
                        rgb.save(output, format="PNG")
                        return output.getvalue()
                finally:
                    bitmap.close()
            finally:
                page.close()
    if kind == "image":
        from PIL import Image, ImageOps, UnidentifiedImageError

        Image.MAX_IMAGE_PIXELS = 20_000_000
        try:
            with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "GIF", "WEBP", "BMP")) as im:
                if im.width * im.height > Image.MAX_IMAGE_PIXELS:
                    raise UnsafeDocument
                im.seek(0)  # no animated-frame expansion
                thumb = ImageOps.exif_transpose(im).convert("RGB")
                thumb.thumbnail((MAX_THUMB_SIDE, MAX_THUMB_SIDE))
                output = io.BytesIO()
                thumb.save(output, format="PNG", optimize=False)
                return output.getvalue()
        except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise UnsafeDocument from exc
    raise UnsafeDocument


def _limit_process() -> None:
    # Apply inside the child before ingesting untrusted bytes/parsing. Doing
    # this here (rather than preexec_fn) is safe in a multithreaded API worker.
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    memory = 768 * 1024 * 1024
    if sys.platform == "darwin":
        # macOS launches Python with >768 MB virtual address reservations;
        # RLIMIT_AS would prevent even the standard library from importing.
        # RLIMIT_DATA covers the heap; CPU and wall clocks remain hard bounds.
        try:
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        except ValueError:
            # Some macOS/Python combinations reject every finite DATA limit
            # with "current limit exceeds maximum limit". Keep CPU/wall,
            # file/input/pixel/zip bounds for local development; Linux
            # deployment always requires its RLIMIT_AS to succeed.
            pass
    else:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"extract", "thumbnail"}:
        return 2
    try:
        _limit_process()
    except (OSError, ValueError):
        return 4
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        if sys.argv[1] == "extract":
            result = extract(data, sys.argv[2])
            output = json.dumps(result, ensure_ascii=False).encode("utf-8")
        else:
            output = thumbnail(data, sys.argv[2])
        if len(output) > 2 * 1024 * 1024:
            raise UnsafeDocument
        sys.stdout.buffer.write(output)
        return 0
    except (UnsafeDocument, zipfile.BadZipFile, KeyError):
        return 3
    except ImportError:
        return 4  # a missing converter/runtime dependency is infrastructure, not bad input
    except Exception as exc:  # noqa: BLE001 - never expose native/parser diagnostics
        import struct
        import zlib
        from xml.etree.ElementTree import ParseError

        from defusedxml.common import DefusedXmlException
        from PIL import Image, UnidentifiedImageError
        from pypdfium2 import PdfiumError

        if isinstance(
            exc,
            (
                ParseError,
                SyntaxError,
                DefusedXmlException,
                PdfiumError,
                UnicodeError,
                struct.error,
                zlib.error,
                UnidentifiedImageError,
                Image.DecompressionBombError,
                ValueError,
                OSError,
                MemoryError,
            ),
        ):
            return 3  # deterministic decode/shape/memory failures are permanent
        return 5  # unknown native failures get bounded retries, not an endless hot loop


if __name__ == "__main__":
    sys.exit(main())
