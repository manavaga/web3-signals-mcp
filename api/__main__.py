# api/__main__.py
"""Entry point: python -m api"""
import os
import uvicorn


def _resolve_port() -> int:
    for var in ("RAILWAY_PORT", "PORT"):
        val = os.getenv(var)
        if val and int(val) != 5432:
            return int(val)
    return 8000


if __name__ == "__main__":
    port = _resolve_port()
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, log_level="info")
