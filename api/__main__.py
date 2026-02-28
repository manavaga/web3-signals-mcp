"""Allow running: python -m api"""
import os
import uvicorn


def _resolve_port() -> int:
    """Resolve the correct port, handling Railway's Postgres PORT override."""
    # RAILWAY_PORT is the internal web port (preferred).
    # PORT might be overridden by Postgres addon to 5432.
    for var in ("RAILWAY_PORT", "PORT"):
        val = os.getenv(var)
        if val:
            try:
                p = int(val)
                if p != 5432:  # Never bind to Postgres
                    return p
            except ValueError:
                pass
    return 8000


port = _resolve_port()
print(f"Starting server on 0.0.0.0:{port} (RAILWAY_PORT={os.getenv('RAILWAY_PORT')}, PORT={os.getenv('PORT')})")
uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
