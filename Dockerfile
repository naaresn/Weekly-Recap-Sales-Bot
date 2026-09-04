# ---- Stage 1: build dependencies ----
FROM python:3.11-slim AS builder

WORKDIR /app

# build-essential dibutuhkan untuk compile beberapa dependency pandas/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---- Stage 2: runtime image ----
FROM python:3.11-slim

WORKDIR /app

# User non-root untuk keamanan production
RUN useradd --create-home --shell /bin/bash appuser

# Salin package yang sudah diinstall dari stage builder
COPY --from=builder /root/.local /home/appuser/.local
COPY . .

RUN chown -R appuser:appuser /app
USER appuser

ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Port untuk health-check server (opsional, bot utamanya jalan via long-polling)
EXPOSE 8080

CMD ["python", "main.py"]
