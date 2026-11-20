"""Turn an uploaded PDF / Word / text file into retrievable passages. Pure functions, no DB or Flask access.

Uses pypdf and python-docx (already dependencies of the upload parsers). Scanned PDFs have no text layer; OCR is deliberately
not attempted here (it pulls in PyTorch) and such files are reported as having no extractable text.
"""
import re
from pathlib import Path

DOC_EXT = {"pdf", "docx", "txt"}
MAX_PAGES = 200
TARGET_CHARS = 700      # passage size: big enough to hold a thought, small enough to rank precisely
OVERLAP_CHARS = 100
MAX_PASSAGES = 2000


def extract(path: Path, ext: str):
    """Return [{"page": int|None, "kind": "paragraph"|"table", "text": str}] in reading order."""
    if ext == "pdf":
        return _pdf(path)
    if ext == "docx":
        return _docx(path)
    if ext == "txt":
        return _txt(path)
    return []


def _clean(t):
    t = re.sub(r"[ \t ]+", " ", str(t or ""))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return "".join(ch for ch in t if ch == "\n" or ch >= " ").strip()


def _pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        return []
    out = []
    for i, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        text = _clean(page.extract_text() or "")
        for para in re.split(r"\n\s*\n", text):
            if para.strip():
                out.append({"page": i, "kind": "paragraph", "text": para.strip()})
    return out


def _docx(path):
    import docx
    d = docx.Document(str(path))
    out = [{"page": None, "kind": "paragraph", "text": _clean(p.text)} for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        header = [c.text.strip() for c in t.rows[0].cells] if t.rows else []
        for row in t.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):  # "Metric: Net revenue; Q3: 12%" keeps each value attached to its column name
                out.append({"page": None, "kind": "table", "text": "; ".join(f"{h or 'col'}: {v}" for h, v in zip(header, cells) if v)})
    return out


def _txt(path):
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return [{"page": None, "kind": "paragraph", "text": p.strip()} for p in re.split(r"\n\s*\n", _clean(text)) if p.strip()]


def chunk(passages, target=TARGET_CHARS, overlap=OVERLAP_CHARS):
    """Merge short paragraphs up to ~target chars (same page), split long ones on sentence boundaries with a small overlap.
    Tables are kept row by row. Returns [{"chunk_idx", "page", "kind", "text"}]."""
    out, buf, buf_page = [], "", None

    def flush():
        nonlocal buf, buf_page
        if buf.strip():
            out.append({"page": buf_page, "kind": "paragraph", "text": buf.strip()})
        buf, buf_page = "", None

    for p in passages:
        if p["kind"] == "table":
            flush()
            out.append({"page": p["page"], "kind": "table", "text": p["text"]})
            continue
        text = p["text"]
        if buf and (p["page"] != buf_page or len(buf) + len(text) + 1 > target):
            flush()
        if len(text) <= target:
            buf, buf_page = (buf + "\n" + text).strip(), p["page"]
            continue
        flush()
        sentences, cur = re.split(r"(?<=[.!?])\s+", text), ""
        for s in sentences:
            if cur and len(cur) + len(s) + 1 > target:
                out.append({"page": p["page"], "kind": "paragraph", "text": cur.strip()})
                cur = cur[-overlap:] + " " + s if overlap else s
            else:
                cur = (cur + " " + s).strip()
        if cur.strip():
            out.append({"page": p["page"], "kind": "paragraph", "text": cur.strip()})
    flush()
    out = out[:MAX_PASSAGES]
    for i, c in enumerate(out):
        c["chunk_idx"] = i
    return out
