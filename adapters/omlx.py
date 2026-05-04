"""
Adapter for oMLX server
oMLX provides OpenAI-compatible API and health endpoint with memory info
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from .base import BaseAdapter

logger = logging.getLogger(__name__)

import os

# Cache directory: /data (docker) or ~/.cache/ai-server-control/ (manual)
_CACHE_DIR = os.environ.get("CONFIG_DIR")  # shares CONFIG_DIR from docker
if _CACHE_DIR:
    SESSION_CACHE_DIR = Path(_CACHE_DIR)
else:
    SESSION_CACHE_DIR = Path.home() / ".cache" / "ai-server-control"
SESSION_CACHE_FILE = SESSION_CACHE_DIR / "omlx_sessions.json"


def _get_cached_session(base_url: str) -> Optional[str]:
    """Get cached session for a base_url from file"""
    try:
        if SESSION_CACHE_FILE.exists():
            sessions = json.loads(SESSION_CACHE_FILE.read_text())
            entry = sessions.get(base_url, {})
            return entry.get("session_cookie")
    except Exception as e:
        logger.warning(f"Failed to read session cache: {e}")
    return None


def _save_session(base_url: str, session_cookie: str):
    """Save session cookie to file"""
    try:
        SESSION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sessions = {}
        if SESSION_CACHE_FILE.exists():
            sessions = json.loads(SESSION_CACHE_FILE.read_text())
        sessions[base_url] = {
            "session_cookie": session_cookie,
            "cached_at": time.time()
        }
        SESSION_CACHE_FILE.write_text(json.dumps(sessions, indent=2))
        logger.info(f"Saved oMLX session for {base_url}")
    except Exception as e:
        logger.warning(f"Failed to save session cache: {e}")


class OmlxAdapter(BaseAdapter):
    """Adapter for oMLX server"""
    
    def __init__(self, base_url: str, api_key: str = ""):
        super().__init__(base_url, api_key)
        self._admin_cookie: Optional[str] = None
        # Try to load cached session
        cached = _get_cached_session(self.base_url)
        if cached:
            self._admin_cookie = cached
            logger.info(f"Loaded cached oMLX session for {base_url}")
    
    async def check_health(self, client: httpx.AsyncClient) -> dict:
        """
        oMLX has /health endpoint with rich info:
        - status: healthy/unhealthy
        - engine_pool.loaded_count: number of loaded models
        - engine_pool.current_model_memory: current memory usage
        - engine_pool.max_model_memory: maximum memory
        """
        try:
            resp = await client.get(f"{self.base_url}/health", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "online" if data.get("status") == "healthy" else "error",
                    "error": None,
                    "total_memory": data.get("engine_pool", {}).get("max_model_memory"),
                    "used_memory": data.get("engine_pool", {}).get("current_model_memory"),
                    "loaded_count": data.get("engine_pool", {}).get("loaded_count")
                }
            return {"status": "error", "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}
    
    async def _get_admin_stats(self, client: httpx.AsyncClient) -> Optional[dict]:
        """
        Try to get detailed stats from /admin/api/stats using cookie auth.
        Returns None if not available.
        """
        # Try using the regular API key as a cookie value
        if self._admin_cookie:
            cookies = {"omlx_admin_session": self._admin_cookie}
        else:
            cookies = {"omlx_admin_session": self.api_key}
        
        try:
            resp = await client.get(
                f"{self.base_url}/admin/api/stats",
                cookies=cookies,
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                # Try login to get session cookie
                login_resp = await client.post(
                    f"{self.base_url}/admin/api/login",
                    json={"api_key": self.api_key},
                    timeout=10
                )
                if login_resp.status_code == 200:
                    # Get session cookie from login response
                    login_cookies = login_resp.cookies
                    new_session = login_cookies.get("omlx_admin_session")
                    resp = await client.get(
                        f"{self.base_url}/admin/api/stats",
                        cookies=login_cookies,
                        timeout=10
                    )
                    if resp.status_code == 200:
                        self._admin_cookie = new_session
                        _save_session(self.base_url, self._admin_cookie)
                        return resp.json()
        except Exception as e:
            logger.error(f"oMLX /admin/api/stats error: {e}")
        return None
    
    async def get_models(self, client: httpx.AsyncClient) -> list[dict]:
        """
        Get models from oMLX
        
        Strategy:
        - Use /v1/models for complete list of all available models
        - Use /admin/api/stats to know which models are loaded and their activity
        - Combine both to show all models with correct load_status and activity_status
        
        load_status: unloaded, loaded, loading
        activity_status: active, idle (only for loaded models)
        """
        # Get admin stats to know loaded models and their activity
        admin_stats = await self._get_admin_stats(client)
        
        # Build dict of loaded models with their activity info
        loaded_models = {}  # model_id -> {load_status, activity_status}
        if admin_stats and "active_models" in admin_stats:
            for m in admin_stats.get("active_models", {}).get("models", []):
                model_id = m.get("id", "")
                is_loading = m.get("is_loading", False)
                active_requests = m.get("active_requests", 0)
                generating = m.get("generating", [])
                prefilling = m.get("prefilling", [])
                
                # Determine load_status
                load_status = "loading" if is_loading else "loaded"
                
                # Determine activity_status (only for loaded models)
                # Model is "active" if it has active requests or is generating/prefilling
                if is_loading:
                    activity_status = None  # loading models don't have activity yet
                elif active_requests > 0 or generating or prefilling:
                    activity_status = "active"
                else:
                    activity_status = "idle"
                
                loaded_models[model_id] = {
                    "load_status": load_status,
                    "activity_status": activity_status
                }
        
        # Get all models from /v1/models
        models = []
        try:
            resp = await client.get(
                f"{self.base_url}/v1/models",
                headers=self.get_headers(),
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    
                    if model_id in loaded_models:
                        info = loaded_models[model_id]
                        load_status = info["load_status"]
                        activity_status = info["activity_status"]
                    else:
                        load_status = "unloaded"
                        activity_status = None
                    
                    models.append({
                        "id": model_id,
                        "name": model_id,
                        "load_status": load_status,
                        "activity_status": activity_status,
                        "memory": None,
                        "context_window": None
                    })
        except Exception as e:
            logger.error(f"oMLX /v1/models error: {e}")
        
        return models
    
    async def _get_admin_cookies(self, client: httpx.AsyncClient) -> dict:
        """Get cookies for admin API (reuses session or logs in)"""
        if self._admin_cookie:
            return {"omlx_admin_session": self._admin_cookie}
        return {"omlx_admin_session": self.api_key}
    
    async def load_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Load a model using POST /admin/api/models/{model_id}/load
        """
        # Ensure we have valid session
        await self._get_admin_stats(client)
        cookies = await self._get_admin_cookies(client)
        
        try:
            resp = await client.post(
                f"{self.base_url}/admin/api/models/{model_id}/load",
                cookies=cookies,
                timeout=60
            )
            if resp.status_code == 200:
                return {"success": True, "error": None}
            return {"success": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def unload_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Unload a model using POST /admin/api/models/{model_id}/unload
        """
        # Ensure we have valid session
        await self._get_admin_stats(client)
        cookies = await self._get_admin_cookies(client)
        
        try:
            resp = await client.post(
                f"{self.base_url}/admin/api/models/{model_id}/unload",
                cookies=cookies,
                timeout=30
            )
            if resp.status_code == 200:
                return {"success": True, "error": None}
            return {"success": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}