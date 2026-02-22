"""Allow running: python -m api.server"""
import os
import uvicorn

port = int(os.getenv("PORT", 8000))
uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
