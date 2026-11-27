"""
JUP OCR Service — layanan OCR lokal (tanpa API berbayar) untuk Sistem Persuratan.

Dipanggil dari n8n lewat node HTTP Request (multipart, field "file").
Mendukung: PDF (teks maupun hasil scan), DOCX, dan gambar (PNG/JPG/TIFF/BMP/WEBP).

Endpoint:
  GET  /health  -> cek layanan + bahasa Tesseract yang tersedia
  POST /ocr     -> ekstrak teks + tebakan field surat (nomor, perihal, tanggal, lampiran)

Env (opsional):
  OCR_LANG       default "ind+eng"
  OCR_DPI        default 300   (resolusi render halaman PDF hasil scan)
  OCR_MAX_PAGES  default 10    (batas halaman yang diproses)
  OCR_API_KEY    kalau diisi, request wajib membawa header X-API-Key yang sama
"""
from __future__ import annotations

import io
import os
import re
import time
import zipfile
from typing import Optional

import pymupdf
import pytesseract
from docx import Document
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from PIL import Image, ImageOps

OCR_LANG = os.getenv("OCR_LANG", "ind+eng")
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "10"))
OCR_API_KEY = os.getenv("OCR_API_KEY", "")
MIN_TEXT_CHARS = 30  # halaman PDF dengan teks < ini dianggap hasil scan -> di-OCR

app = FastAPI(title="JUP OCR Service", version="1.0.0")


# ---------------------------------------------------------------- OCR helpers
def _prep(img: Image.Image) -> Image.Image:
    """Pra-proses ringan: grayscale, kontras otomatis, perbesar gambar kecil."""
    img = ImageOps.exif_transpose(img).convert("L")
    img = ImageOps.autocontrast(img)
    if img.width < 1500:
        scale = 1500 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def ocr_image(img: Image.Image, lang: str) -> tuple[str, Optional[float]]:
    """OCR satu gambar. Mengembalikan (teks, rata-rata confidence 0-100)."""
    img = _prep(img)
    data = pytesseract.image_to_data(
        img, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT
    )
    confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    text = pytesseract.image_to_string(img, lang=lang, config="--psm 3")
    conf = round(sum(confs) / len(confs), 1) if confs else None
    return text.strip(), conf


def from_pdf(raw: bytes, lang: str, force_ocr: bool) -> tuple[str, list[dict]]:
    doc = pymupdf.open(stream=raw, filetype="pdf")
    texts, pages = [], []
    for i, page in enumerate(doc):
        if i >= OCR_MAX_PAGES:
            break
        native = page.get_text().strip()
        if native and len(native) >= MIN_TEXT_CHARS and not force_ocr:
            texts.append(native)
            pages.append({"page": i + 1, "method": "text-layer", "chars": len(native), "confidence": None})
            continue
        pix = page.get_pixmap(dpi=OCR_DPI)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text, conf = ocr_image(img, lang)
        texts.append(text)
        pages.append({"page": i + 1, "method": "ocr", "chars": len(text), "confidence": conf})
    return "\n\n".join(texts), pages


def from_docx(raw: bytes, lang: str) -> tuple[str, list[dict]]:
    d = Document(io.BytesIO(raw))
    parts = []
    for section in d.sections:
        parts += [p.text for p in section.header.paragraphs if p.text.strip()]
    parts += [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))  # buang sel gabungan yang dobel
    text = "\n".join(parts).strip()
    pages = [{"page": 1, "method": "docx-text", "chars": len(text), "confidence": None}]

    # Word yang isinya cuma gambar scan -> OCR gambar di word/media/
    if len(text) < MIN_TEXT_CHARS:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            media = [n for n in z.namelist() if n.startswith("word/media/")]
            for n, name in enumerate(media[:OCR_MAX_PAGES], start=1):
                try:
                    img = Image.open(io.BytesIO(z.read(name)))
                except Exception:
                    continue
                t, conf = ocr_image(img, lang)
                text += ("\n\n" if text else "") + t
                pages.append({"page": n, "method": "docx-image-ocr", "chars": len(t), "confidence": conf})
    return text, pages


# --------------------------------------------------------- field extraction
BULAN = (
    "januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember|"
    "jan|feb|mar|apr|jun|jul|agu|agt|sep|okt|nov|des"
)
# [\s_\-.,;']* menoleransi noda OCR antara label dan titik dua (mis. "Nomor _ :")
JUNK = r"[\s_\-.,;'`]*"
RE_NOMOR = re.compile(rf"^\s*(?:nomor|no){JUNK}:\s*(.+)$", re.I | re.M)
RE_PERIHAL = re.compile(rf"^\s*(?:perihal|hal){JUNK}:\s*(.+)$", re.I | re.M)
RE_LAMPIRAN = re.compile(rf"^\s*(?:lampiran|lamp){JUNK}:\s*(.+)$", re.I | re.M)
RE_TANGGAL = re.compile(rf"\b(\d{{1,2}})\s+({BULAN})\.?\s+(\d{{4}})\b", re.I)


def _first(rx: re.Pattern, text: str) -> Optional[str]:
    m = rx.search(text)
    return m.group(1).strip(" :.-\t") if m else None


def extract_fields(text: str) -> dict:
    """Tebakan field surat berbasis pola umum surat dinas Indonesia (heuristik, bukan AI)."""
    tgl = RE_TANGGAL.search(text)
    return {
        "nomor_surat": _first(RE_NOMOR, text),
        "perihal": _first(RE_PERIHAL, text),
        "lampiran": _first(RE_LAMPIRAN, text),
        "tanggal": " ".join(tgl.groups()) if tgl else None,
    }


# ------------------------------------------------------------------ routes
def detect_type(raw: bytes, filename: str) -> str:
    if raw[:4] == b"%PDF":
        return "pdf"
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
    try:
        Image.open(io.BytesIO(raw)).verify()
        return "image"
    except Exception:
        pass
    if filename.lower().endswith(".doc"):
        raise HTTPException(415, "Format .doc lama tidak didukung. Simpan ulang sebagai .docx atau PDF.")
    raise HTTPException(415, "Format tidak didukung. Gunakan PDF, DOCX, atau gambar.")


@app.get("/health")
def health():
    return {"status": "ok", "tesseract": str(pytesseract.get_tesseract_version()),
            "languages": pytesseract.get_languages(config="")}


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    lang: str = Query(OCR_LANG, description="Bahasa Tesseract, mis. ind+eng"),
    force_ocr: bool = Query(False, description="Paksa OCR walau PDF punya lapisan teks"),
    x_api_key: Optional[str] = Header(None),
):
    if OCR_API_KEY and x_api_key != OCR_API_KEY:
        raise HTTPException(401, "X-API-Key salah atau tidak ada.")
    t0 = time.perf_counter()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "File kosong.")
    kind = detect_type(raw, file.filename or "")

    if kind == "pdf":
        text, pages = from_pdf(raw, lang, force_ocr)
    elif kind == "docx":
        text, pages = from_docx(raw, lang)
    else:
        t, conf = ocr_image(Image.open(io.BytesIO(raw)), lang)
        text, pages = t, [{"page": 1, "method": "ocr", "chars": len(t), "confidence": conf}]

    return {
        "filename": file.filename,
        "file_type": kind,
        "used_ocr": any(p["method"].endswith("ocr") for p in pages),
        "pages": pages,
        "fields": extract_fields(text),
        "text": text,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
