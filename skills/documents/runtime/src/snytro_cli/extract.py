# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from collections import Counter
from pathlib import Path


IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
SUPPORTED = {".pdf", ".docx", ".txt", ".text", ".md", ".rst", ".csv", ".tsv", ".json", ".html", ".htm", ".xlsx", ".pptx", ".xml", ".drawio"} | IMAGES

# Standard lexical stopwords prevent common function words dominating rank fusion.
# This is tokenization, not task/planner routing; names, numbers and negation remain.
SEARCH_STOPWORDS = frozenset("a an and are as at be been being by can could do does for from had has have how i in into is it its me much my of on or our should than that the their them these they this those to was we were what when where which who why will with would you your".split())


def discover(paths: list[str], recursive: bool = True) -> tuple[list[Path], list[dict]]:
    files, errors, seen = [], [], set()
    for raw in paths:
        try:
            root = Path(raw).expanduser().resolve(strict=True)
            if not root.is_file() and not root.is_dir():
                raise ValueError("Path must be a regular file or directory.")
            candidates = root.rglob("*") if root.is_dir() and recursive else root.glob("*") if root.is_dir() else [root]
            for item in candidates:
                # Named symlinks resolve above; discovered symlinks never expand folder scope.
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix.lower() not in SUPPORTED:
                    if root.is_file():
                        errors.append({"path": str(item), "error": "Unsupported file type."})
                    continue
                resolved = item.resolve(strict=True)
                if root.is_dir() and not resolved.is_relative_to(root):
                    continue
                if resolved not in seen:
                    files.append(resolved)
                    seen.add(resolved)
        except (OSError, ValueError) as exc:
            errors.append({"path": raw, "error": str(exc)})
    return sorted(files), errors


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_output(argv, failure):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=120, check=True)
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        raise ValueError(failure) from None


def ocr(path):
    executable = shutil.which("tesseract")
    if not executable:
        raise ValueError("OCR requires Tesseract on PATH.")
    return tool_output([executable, str(path), "stdout"], "Tesseract could not read the source image.")


def pdf_contents(path, rich):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    contents = []
    rasteriser = shutil.which("pdftoppm") if rich else None
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        locator = {"kind": "page", "page": number}
        if rasteriser and shutil.which("tesseract") and len(text.strip()) < 40:
            with tempfile.TemporaryDirectory(prefix="snytro-ocr-") as directory:
                stem = Path(directory) / "page"
                tool_output([rasteriser, "-f", str(number), "-l", str(number), "-r", "200",
                             "-singlefile", "-png", str(path), str(stem)], "Could not render the PDF page for OCR.")
                recognised = ocr(stem.with_suffix(".png"))
                if len(recognised.strip()) > len(text.strip()):
                    text = recognised
                    locator["extraction"] = "ocr"
        contents.append((text, locator))
    return contents


def slide_contents(path):
    # Use presentation relationship order rather than filename order after edits.
    from posixpath import normpath
    from html import unescape
    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with ZipFile(path) as archive:
        def read(name):
            if archive.getinfo(name).file_size > 16 * 1024 * 1024:
                raise ValueError("A PowerPoint XML part exceeds the extraction limit.")
            return ET.fromstring(archive.read(name))
        presentation = read("ppt/presentation.xml")
        relationships = {row.get("Id"): row.get("Target") for row in read("ppt/_rels/presentation.xml.rels")}
        slides = []
        for n, slide in enumerate(presentation.findall(".//p:sldId", ns), 1):
            target = relationships.get(slide.get("{" + ns["r"] + "}id"), "")
            name = normpath(target.lstrip("/") if target.startswith("/") else "ppt/" + target)
            if not name.startswith("ppt/slides/"):
                raise ValueError("PowerPoint slide has an invalid internal target.")
            text = "\n".join(node.text for node in read(name).findall(".//a:t", ns) if node.text)
            slides.append((unescape(text), {"kind": "slide", "slide": n}))
        return slides


def diagram_contents(path):
    import base64
    import zlib
    from urllib.parse import unquote
    from bs4 import BeautifulSoup
    element = ET.parse(path).getroot()
    diagrams = element.findall("diagram") if element.tag == "mxfile" else [element]
    contents = []
    for number, diagram in enumerate(diagrams, 1):
        if len(diagram) == 0 and diagram.text and diagram.tag == "diagram":
            compressed = base64.b64decode(diagram.text.strip(), validate=True)
            decoder = zlib.decompressobj(-15)
            data = decoder.decompress(compressed, 16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024 or not decoder.eof:
                raise ValueError("Compressed diagram is invalid or too large.")
            diagram = ET.fromstring(unquote(data.decode("utf-8")))
        values = [value for node in diagram.iter() for attr in ("value", "label") if (value := node.get(attr))]
        text = "\n".join(BeautifulSoup(value, "html.parser").get_text(" ", strip=True) for value in values)
        contents.append((text, {"kind": "diagram", "diagram": number}))
    return contents


def extract_units(path: Path, *, rich=False) -> list[dict]:
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("File exceeds the 128 MiB per-file extraction limit.")
    extension = path.suffix.lower()
    contents = []
    if extension == ".pdf":
        contents = pdf_contents(path, rich)
    elif extension == ".docx":
        office = shutil.which("soffice") if rich else None
        if office:
            with tempfile.TemporaryDirectory(prefix="snytro-layout-") as directory:
                root = Path(directory)
                tool_output([office, "-env:UserInstallation=" + (root / "profile").as_uri(), "--headless",
                             "--convert-to", "pdf", "--outdir", directory, str(path)], "Could not render DOCX pages with LibreOffice.")
                rendered = root / (path.stem + ".pdf")
                if not rendered.is_file():
                    raise ValueError("LibreOffice did not produce the DOCX PDF rendition.")
                contents = [(text, {**locator, "rendition": "LibreOffice PDF"}) for text, locator in pdf_contents(rendered, rich)]
        else:
            contents = docx_contents(path)
    elif extension == ".pptx":
        contents = slide_contents(path)
    elif extension == ".drawio":
        contents = diagram_contents(path)
    elif extension in IMAGES:
        contents = [(ocr(path), {"kind": "image", "extraction": "ocr"})]
    elif extension == ".xlsx":
        contents = spreadsheet_contents(path, rich)
    else:
        raw = path.read_text(encoding="utf-8-sig")
        if extension in {".html", ".htm"}:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "html.parser")
            for node in soup(["script", "style"]):
                node.decompose()
            raw = soup.get_text("\n")
        elif extension == ".json":
            raw = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        elif extension in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(raw), delimiter="\t" if extension == ".tsv" else ",")
            raw = "\n".join("\t".join(row) for row in reader)
        if rich and extension in {".txt", ".text", ".md", ".rst", ".xml"}:
            lines = raw.splitlines()
            contents = [("\n".join(lines[start:start + 80]),
                         {"kind": "lines", "line_start": start + 1, "line_end": min(start + 80, len(lines))})
                        for start in range(0, len(lines), 80)]
        else:
            contents.append((raw, {"kind": "document"}))
    units = [{"number": i, "content": text.replace("\x00", ""), "locator": locator} for i, (text, locator) in enumerate(contents, 1)]
    if not any(unit["content"].strip() for unit in units):
        raise ValueError("No extractable text. Scanned PDFs require OCR with --rich, Poppler and Tesseract.")
    return units


def docx_contents(path):
    from docx import Document
    from docx.table import Table
    document = Document(path)
    # Preserve body order; Word layout does not expose reliable physical page numbers.
    text = []
    for element in document.iter_inner_content():
        if isinstance(element, Table):
            text.extend("\t".join(cell.text for cell in row.cells) for row in element.rows)
        else:
            text.append(element.text)
    return [("\n".join(text), {"kind": "document"})]


def spreadsheet_contents(path, rich=False):
    contents = []
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    workbook = load_workbook(path, read_only=True, data_only=False)
    displayed = None
    try:
        if rich:
            displayed = load_workbook(path, read_only=True, data_only=True)
        for sheet in workbook:
            if rich:
                block, length = [], 0

                def save_range():
                    if block:
                        start, end = block[0][0], block[-1][0]
                        last_column = get_column_letter(max(row[2] for row in block))
                        area = f"A{start}:{last_column}{end}"
                        text = f"Worksheet: {sheet.title}\nRange: {area}\n\n" + "\n".join(row[1] for row in block)
                        contents.append((text, {"kind": "sheet", "sheet": sheet.title, "range": area,
                                                "row_start": start, "row_end": end}))

                for number, (row, values) in enumerate(zip(sheet.values, displayed[sheet.title].values), 1):
                    cells = [f"{formula} => {value}" if isinstance(formula, str) and formula.startswith("=") and value is not None
                             else str(formula) if formula is not None else "" for formula, value in zip(row, values)]
                    width = max((i for i, value in enumerate(cells, 1) if value), default=0)
                    if not width:
                        continue
                    row_text = f"Row {number}: " + " | ".join(cells[:width])
                    if block and (len(block) >= 50 or length + len(row_text) > 6000):
                        save_range()
                        block, length = [], 0
                    block.append((number, row_text, width))
                    length += len(row_text)
                save_range()
                continue
            rows = []
            start = 1
            for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                rows.append("\t".join("" if value is None else str(value) for value in row))
                if len(rows) == 100:
                    contents.append(("\n".join(rows), {"kind": "sheet", "sheet": sheet.title, "row_start": start, "row_end": number}))
                    rows, start = [], number + 1
            if rows:
                contents.append(("\n".join(rows), {"kind": "sheet", "sheet": sheet.title, "row_start": start, "row_end": start + len(rows) - 1}))
    finally:
        workbook.close()
        if displayed is not None:
            displayed.close()
    return contents


def terms(text: str) -> list[str]:
    words, word = [], []
    for char in unicodedata.normalize("NFC", text).lower():
        if char.isalnum() or word and unicodedata.category(char).startswith("M"):
            word.append(char)
        elif word:
            words.append("".join(word))
            word = []
    if word:
        words.append("".join(word))
    return words


def overview(text: str) -> dict:
    # Bootstrap excerpts only; the skill replaces them with host-generated analysis.
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    return {
        "tagline": (sentences[0] if sentences else "")[:180],
        "summary": "\n".join(sentences[:8])[:2400],
        "keywords": [word for word, _ in Counter(word for word in terms(text) if len(word) > 3).most_common(20)],
    }


def text_chunks(text: str) -> list[str]:
    return [text[start:start + 1600] for start in range(0, len(text), 1400) if text[start:start + 1600].strip()]
