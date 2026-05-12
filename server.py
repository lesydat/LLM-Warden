"""
LLM Warden - Backend
FastAPI server for monitoring AI inference servers (oMLX, llama.cpp, Ollama)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters import get_adapter

import os

# Config directory: /data (docker) or ./ (manual)
_CONFIG_DIR = os.environ.get("CONFIG_DIR")
if _CONFIG_DIR:
    CONFIG_DIR = Path(_CONFIG_DIR)
else:
    CONFIG_DIR = Path(__file__).parent

CONFIG_FILE = CONFIG_DIR / "config.json"

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load config
with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

app = FastAPI(title="LLM Warden")


# ============ Models ============

class ModelInfo(BaseModel):
    id: str
    name: str
    load_status: str  # unloaded, loaded, loading
    activity_status: Optional[str] = None  # active, idle (None if not available)
    vram_size: Optional[int] = None  # bytes - actual VRAM used (size_vram from /api/ps)
    memory_size: Optional[int] = None  # bytes - total memory including KV cache (size from /api/ps)
    model_size: Optional[int] = None  # bytes - model file size
    context_window: Optional[int] = None
    engine_type: Optional[str] = None  # model_type for oMLX (LLM, VLM, OCR, etc.)
    supports: Optional[list[str]] = None  # capabilities list: Ollama uses [Vision, Tools, Thinking, Text, Embedding, Cloud]; oMLX uses [LLM, VLM, OCR, Embedding, Reranker]


class ServerInfo(BaseModel):
    name: str
    type: str
    type_name: str
    status: str  # online, offline, error
    endpoint: str
    models: list[ModelInfo]
    total_memory: Optional[int] = None
    used_memory: Optional[int] = None
    error: Optional[str] = None


class MonitorResponse(BaseModel):
    servers: list[ServerInfo]
    timestamp: str


class ModelActionRequest(BaseModel):
    model_id: str


class ModelActionResponse(BaseModel):
    success: bool
    error: Optional[str] = None


# ============ API Endpoints ============

@app.get("/api/status", response_model=MonitorResponse)
async def get_status():
    """Get status of all servers in parallel for better performance"""
    
    async def fetch_server(server: dict) -> ServerInfo:
        adapter = get_adapter(
            server_type=server["type"],
            base_url=server["base_url"],
            api_key=server.get("api_key", "")
        )
        async with httpx.AsyncClient() as client:
            health = await adapter.check_health(client)
            raw_models = await adapter.get_models(client) if health["status"] == "online" else []
            models = [ModelInfo(**m) for m in raw_models]
            return ServerInfo(
                name=server["name"],
                type=server["type"],
                type_name=server.get("type_name", server["name"]),
                status=health["status"],
                endpoint=server["base_url"],
                models=models,
                total_memory=health.get("total_memory"),
                used_memory=health.get("used_memory"),
                error=health.get("error")
            )
    
    # Fetch all servers in parallel
    servers = await asyncio.gather(*[fetch_server(s) for s in CONFIG["servers"]])
    
    return MonitorResponse(
        servers=list(servers),
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@app.get("/config")
async def get_config():
    """Get non-sensitive config (public info only)"""
    return {
        "servers": [
            {"name": s["name"], "type": s["type"], "type_name": s.get("type_name", s["name"]), "endpoint": s["base_url"]}
            for s in CONFIG["servers"]
        ],
        "refresh_intervals": CONFIG["refresh_intervals"],
        "default_refresh": CONFIG["default_refresh"]
    }


@app.post("/api/models/{server_type}/{server_name}/load", response_model=ModelActionResponse)
async def load_model(server_type: str, server_name: str, request: ModelActionRequest):
    """Load a model on a specific server"""
    # Find server config
    server_config = None
    for s in CONFIG["servers"]:
        if s["name"] == server_name and s["type"] == server_type:
            server_config = s
            break
    
    if not server_config:
        return ModelActionResponse(success=False, error=f"Server not found: {server_name}")
    
    adapter = get_adapter(
        server_type=server_config["type"],
        base_url=server_config["base_url"],
        api_key=server_config.get("api_key", "")
    )
    
    async with httpx.AsyncClient() as client:
        result = await adapter.load_model(client, request.model_id)
    
    return ModelActionResponse(**result)


@app.post("/api/models/{server_type}/{server_name}/unload", response_model=ModelActionResponse)
async def unload_model(server_type: str, server_name: str, request: ModelActionRequest):
    """Unload a model from a specific server"""
    # Find server config
    server_config = None
    for s in CONFIG["servers"]:
        if s["name"] == server_name and s["type"] == server_type:
            server_config = s
            break
    
    if not server_config:
        return ModelActionResponse(success=False, error=f"Server not found: {server_name}")
    
    adapter = get_adapter(
        server_type=server_config["type"],
        base_url=server_config["base_url"],
        api_key=server_config.get("api_key", "")
    )
    
    async with httpx.AsyncClient() as client:
        result = await adapter.unload_model(client, request.model_id)
    
    return ModelActionResponse(**result)


# ============ Frontend ============

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML"""
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text())


# Mount static files
static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)