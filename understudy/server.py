"""HTTP and WebSocket server for the UI."""
from fastapi import FastAPI

app = FastAPI(title="understudy")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
