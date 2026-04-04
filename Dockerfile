FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev libffi-dev libgmp-dev make curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD python -c "import os; p=int(os.getenv('RAILWAY_PORT',os.getenv('PORT',8000))); \
        p=8000 if p==5432 else p; \
        from urllib.request import urlopen; urlopen(f'http://localhost:{p}/health',timeout=10)" \
    || exit 1

CMD ["python", "-m", "api"]
