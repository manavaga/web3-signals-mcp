# Web3 Signals x402 — Single-service container
# FastAPI + background orchestrator (runs all 5 agents every 15 min)

FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Install system deps (psycopg2, x402 EVM crypto libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    libffi-dev \
    libgmp-dev \
    make \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Default port — Railway may override via env var
# Note: if Postgres addon sets PORT=5432, our __main__.py handles this
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Health check — uses same PORT logic as __main__.py
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD python -c "import os; p=int(os.getenv('RAILWAY_PORT',os.getenv('PORT',8000))); p=8000 if p==5432 else p; from urllib.request import urlopen; urlopen(f'http://localhost:{p}/health',timeout=10)" || exit 1

# Run the FastAPI server (includes background orchestrator)
# Uses Python to read PORT env var (avoids shell expansion issues on Railway)
CMD ["python", "-m", "api"]
