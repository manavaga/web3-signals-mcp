"""Allow running: python -m api.server"""
import os
import uvicorn

# Railway injects PORT for the web service.
# However, Postgres addon can sometimes override PORT with 5432.
# Use RAILWAY_PORT (internal web port) first, then PORT, then fallback 8000.
port = int(os.getenv("RAILWAY_PORT", os.getenv("PORT", 8000)))

# Safety: never bind to 5432 (that's Postgres)
if port == 5432:
    port = 8000

print(f"Starting server on port {port}")
uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
