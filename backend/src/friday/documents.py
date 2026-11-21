"""Reading attached files: what kind each one is, and the text of documents.

PDF, Word (.docx) and Excel (.xlsx) need the optional ``documents`` extra
(pypdf, python-docx, openpyxl); plain-text files need nothing.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from friday.core.errors import FridayError

IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac"}
TEXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".htm"}
OFFICE = {".pdf", ".docx", ".xlsx"}
DOCUMENT = TEXT | OFFICE


def category(name: str) -> str | None:
    """``image``, ``audio``, ``document``, or None for a kind FRIDAY can't use."""
    ext = Path(name).suffix.lower()
    if ext in IMAGE:
        return "image"
    if ext in AUDIO:
        return "audio"
    if ext in DOCUMENT:
        return "document"
    return None


class DocumentError(FridayError):
    pass


def extract_text(path: Path) -> str:
    """The text of a document, as plainly as possible."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            text = _pdf(path)
        elif ext == ".docx":
            text = _docx(path)
        elif ext == ".xlsx":
            text = _xlsx(path)
        elif ext in TEXT:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise DocumentError(f"I can't read {ext or 'this kind of'} files yet.")
    except DocumentError:
        raise
    except ImportError as exc:
        raise DocumentError(
            f'Reading {ext} files needs an extra part: pip install -e ".[documents]" ({exc.name})'
        ) from exc
    except Exception as exc:
        raise DocumentError(f"I couldn't read {path.name}: {exc}") from exc
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not text:
        hint = " It may be a scan (a picture of pages)." if ext == ".pdf" else ""
        raise DocumentError(f"I found no text in {path.name}.{hint}")
    return text


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for number, page in enumerate(reader.pages, 1):
        words = (page.extract_text() or "").strip()
        if words:
            pages.append(f"[page {number}]\n{words}")
    return "\n\n".join(pages)


def _docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        parts.append("\n".join(rows))
    return "\n".join(parts)


def _xlsx(path: Path) -> str:
    import openpyxl

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    try:
        for sheet in book.worksheets:
            out = io.StringIO()
            writer = csv.writer(out)
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    writer.writerow(["" if cell is None else cell for cell in row])
            if out.getvalue().strip():
                parts.append(f"[sheet {sheet.title}]\n{out.getvalue()}")
    finally:
        book.close()
    return "\n\n".join(parts)


def for_model(name: str, text: str, limit: int) -> tuple[str, bool]:
    """The document as the talking model reads it, cut to ``limit`` characters.

    Returns the text and whether it was cut.
    """
    cut = len(text) > limit
    body = text[:limit] if cut else text
    note = (
        f"\n[... the rest was left out: only the first {limit:,} of {len(text):,} characters fit]"
        if cut
        else ""
    )
    return f'[Attached document "{name}"]\n{body}{note}\n[End of "{name}"]', cut
