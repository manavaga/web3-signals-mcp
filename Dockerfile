# Web3 Signals x402 — Single-service container
# FastAPI + background orchestrator (runs all 5 agents every 15 min)

FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Install system deps (for psycopg2-binary and general build)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Railway injects PORT automatically (default 8000)
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Health check — hit the /health endpoint
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:${PORT}/health')" || exit 1

# Run the FastAPI server (includes background orchestrator)
CMD uvicorn api.server:app --host 0.0.0.0 --port ${PORT}
