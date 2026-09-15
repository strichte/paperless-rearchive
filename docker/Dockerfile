# paperless-rearchive sidecar image.
#
# CPU-only: ocrmypdf + ghostscript + tesseract (for OSD) run here; all LLM
# inference happens on the remote Chandra server.
#
# Base matches paperless-ngx's current image (python:3.14-slim, Debian
# trixie). jbig2 (bilevel image compression for ocrmypdf) is available as an
# apt package in trixie; measured ~15% archive size saving on a real scan
# vs. no JBIG2 compression.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_THREAD_LIMIT=1

# Ghostscript (PDF/A), tesseract (OSD in chandra plugin), qpdf, pngquant,
# jbig2 (bilevel image compression, reinstated in trixie); poppler-utils
# (pdftotext: text-layer detection + content fallback). Tesseract language
# packs are unnecessary (Chandra is language-agnostic) but osd.traineddata
# is needed for rotate_pages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ghostscript \
        qpdf \
        pngquant \
        tesseract-ocr \
        poppler-utils \
        curl \
        git \
        jbig2 \
    && jbig2 --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer: paperless-chandra plugin + runtime deps.
COPY pyproject.toml README.md LICENSE ./
COPY paperless_rearchive ./paperless_rearchive
RUN pip install --no-cache-dir ".[chandra,db]"

# Run as the same UID/GID as the paperless container so it can replace
# archive files on the bind mount.
RUN useradd --uid 1001 --user-group --no-create-home rearchive || true
USER 1001

ENTRYPOINT ["python", "-m", "paperless_rearchive.poller"]
