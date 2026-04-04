# api/__main__.py
"""Entry point: python -m api"""
import os
import uvicorn


def _resolve_port() -> int:
    # Railway sets PORT for the service. Skip 5432 (Postgres addon).
    port = os.getenv("PORT", "8000")
    p = int(port)
    if p == 5432:
        return 8000
    return p


if __name__ == "__main__":
    port = _resolve_port()
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, log_level="info")
