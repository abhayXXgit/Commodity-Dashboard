# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# build-essential: wheels that may need compiling on slim
# fonts-dejavu-core: DejaVu Sans carries U+20B9, so the PDF report prints a real
#   rupee sign instead of falling back to "Rs." (the core PDF fonts have no glyph)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY backend/   ./backend/
COPY frontend/  ./frontend/
COPY database/  ./database/
COPY data/      ./data/

RUN mkdir -p /app/data/imports /app/data/historical /app/reports \
    && useradd -m -u 10001 procure \
    && chown -R procure:procure /app
USER procure

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "backend/main.py", "--host", "0.0.0.0", "--port", "8000"]
