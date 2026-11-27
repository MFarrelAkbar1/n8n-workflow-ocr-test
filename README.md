# JUP OCR Service (Python + Tesseract) untuk n8n

Layanan OCR lokal untuk Sistem Persuratan. Gratis, tanpa API key, dan file surat tidak keluar dari laptop/server.
n8n memanggilnya lewat node **HTTP Request**.

## Kenapa layanan terpisah, bukan node Code Python?
n8n 2.x memang punya Code node Python (native), tapi OCR butuh binary Tesseract dan library tambahan.
Artinya harus membangun image task-runner custom (`n8nio/runners`) dan mengatur allow-list import, yang masih
sering bermasalah. Layanan terpisah lebih stabil, bisa diuji sendiri lewat `/docs`, dan gampang dipindah ke server.

## Isi folder
| File | Fungsi |
|---|---|
| `app.py` | API FastAPI: `/health`, `/ocr` |
| `Dockerfile` | Python 3.12 + Tesseract (bahasa `ind` + `eng`) |
| `docker-compose.yml` | Menjalankan `jup-ocr` (dan n8n kalau mau sekalian) |
| `n8n-workflow-ocr-test.json` | Workflow uji: Form unggah → OCR → ringkasan |
| `samples/` | Surat contoh: DOCX, PDF teks, PDF hasil scan, foto JPG |

## Cara menjalankan
```bash
docker compose up -d --build            # OCR + n8n
# atau, kalau n8n sudah jalan sendiri:
docker compose up -d --build jup-ocr
```
Cek: buka http://localhost:8077/docs → coba `POST /ocr` dengan file dari `samples/`.

```bash
curl -F "file=@samples/surat_scan.pdf" http://localhost:8077/ocr
```

## Cara kerja
- **PDF**: tiap halaman dicek. Kalau punya lapisan teks → diambil langsung (cepat, akurat). Kalau tidak (hasil scan) → dirender 300 dpi lalu di-OCR.
- **DOCX**: teks dibaca langsung (termasuk header dan tabel). Tidak perlu diubah ke PDF dulu. Kalau isinya hanya gambar scan → gambarnya di-OCR.
- **Gambar**: grayscale + kontras otomatis + diperbesar, lalu OCR.
- **Field surat** (`nomor_surat`, `perihal`, `lampiran`, `tanggal`) ditebak dengan pola baku surat dinas ("Nomor :", "Perihal :" / "Hal :", "24 November 2026"). Ini heuristik, bukan AI.
- `.doc` lama ditolak (HTTP 415). Minta pengguna simpan ulang sebagai `.docx`/PDF.

Contoh respons:
```json
{
  "filename": "surat_scan.pdf", "file_type": "pdf", "used_ocr": true,
  "pages": [{"page": 1, "method": "ocr", "chars": 482, "confidence": 94.2}],
  "fields": {"nomor_surat": "045/JUP-IT/XI/2026",
             "perihal": "Permohonan Pengadaan Laptop untuk Divisi Keuangan",
             "lampiran": "1 (satu) berkas", "tanggal": "24 November 2026"},
  "text": "PT JAKARTA UTILITAS PROPERTINDO ...", "duration_ms": 1946
}
```

## Hasil uji (24 Nov 2026, surat contoh)
| File | Metode | Confidence | Waktu | 4 field benar |
|---|---|---|---|---|
| surat.docx | docx-text | – | 17 ms | ✓ |
| surat.pdf | text-layer | – | 2 ms | ✓ |
| surat_scan.pdf (miring 1,2°, blur, noise) | ocr | 94,2 | ~2 dtk | ✓ |
| foto_surat.jpg | ocr | 92,6 | ~1,3 dtk | ✓ |

Belum diuji dengan surat asli JUP (kop surat, stempel, tanda tangan, tabel). Itu langkah uji berikutnya.

## Menyambungkan ke workflow v2 "JUP - Isi Metadata Surat dengan Claude (SharePoint)"
Sisipkan node HTTP Request (salin dari workflow uji) **setelah node unduh file**:

1. URL `http://jup-ocr:8000/ocr` (atau `http://host.docker.internal:8077/ocr`), Body multipart, field `file` = binary dari node unduh.
2. Langkah konversi Word → PDF bisa dihapus, karena DOCX sudah dibaca langsung.
3. Pilih salah satu:
   - **Tanpa AI**: `Title` = `fields.perihal`. Document Type/Departement diisi lewat aturan kata kunci (mis. "undangan" → Undangan) atau dibiarkan untuk diisi manual.
   - **Hemat AI**: kirim `text` (bukan file PDF/gambar) ke Claude. Token lebih sedikit, dan gambar surat tidak ikut terkirim.
4. Kalau `confidence` < 70 atau `perihal` kosong → jangan PATCH; tandai untuk dicek manual.

## Batasan
- Tulisan tangan dan stempel tidak terbaca dengan baik oleh Tesseract.
- Tata letak rumit (kolom, tabel kompleks) bisa membuat urutan teks berantakan. Kalau ini sering terjadi, alternatifnya PaddleOCR (lebih akurat, image lebih besar ±1–2 GB).
- Halaman diproses maks. 10 (`OCR_MAX_PAGES`).
- Kalau dipindah ke server, aktifkan `OCR_API_KEY` dan kirim header `X-API-Key` dari n8n.
