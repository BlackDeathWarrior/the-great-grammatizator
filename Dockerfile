FROM python:3.11-slim

# System deps:
#   tesseract  -> OCR fallback for scanned PDFs (ARCHITECTURE.md §2, TC-0102)
#   ffmpeg     -> video package stitching + whisper audio (§9, TC-0704)
#   poppler    -> pdf rasterisation for the OCR path
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        ffmpeg \
        poppler-utils \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -e ".[dev]"

COPY app ./app
COPY tests ./tests

RUN mkdir -p /data/storage

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
