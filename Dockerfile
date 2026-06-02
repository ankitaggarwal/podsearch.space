FROM python:3.11-slim
# ffmpeg: compresses oversize podcasts (>45 MB) before submitting to the gateway's
#   /v1/transcribe-url (hard 50 MB cap). 16 kHz mono Opus 32 kbps output.
# ca-certificates: the system trust store, needed for TLS to Postgres
#   (sslmode=verify-full), Qdrant, and the inference gateway.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
