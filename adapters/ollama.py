"""
Adapter for Ollama server
Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

import asyncio
import time
from typing import Optional

import logging

import httpx
from .base import BaseAdapter

logger = logging.getLogger(__name__)

# Module-level cache singleton for model capabilities (shared across all adapter instances)
_MODEL_SHOW_CACHE: dict[str, tuple[dict, float]] = {}

# Cache TTL in seconds (capabilities don't change often)
_MODEL_SHOW_CACHE_TTL = 300  # 5 minutes


def _get_cached_model_show(model_id: str) -> Optional[dict]:
    """Get cached model show data if not expired."""
    now = time.time()
    if model_id in _MODEL_SHOW_CACHE:
        cached_data, cached_at = _MODEL_SHOW_CACHE[model_id]
        if now - cached_at < _MODEL_SHOW_CACHE_TTL:
            return cached_data
    return None


def _set_cached_model_show(model_id: str, data: dict) -> None:
    """Cache model show data."""
    _MODEL_SHOW_CACHE[model_id] = (data, time.time())


def _clear_expired_cache() -> int:
    """Remove expired entries from cache. Returns count of removed entries."""
    now = time.time()
    expired = [k for k, (_, cached_at) in _MODEL_SHOW_CACHE.items() if now - cached_at >= _MODEL_SHOW_CACHE_TTL]
    for k in expired:
        del _MODEL_SHOW_CACHE[k]
    return len(expired)


class OllamaAdapter(BaseAdapter):
    """Adapter for Ollama server"""
    
    async def check_health(self, client: httpx.AsyncClient) -> dict:
        """
        Ollama doesn't have /health endpoint, use /api/tags instead
        """
        try:
            resp = await client.get(
                f"{self.base_url}/api/tags",
                headers=self.get_headers(),
                timeout=5
            )
            if resp.status_code == 200:
                return {"status": "online", "error": None}
            return {"status": "error", "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}
    
    # Module-level cache shared across all adapter instances (see module-level vars above)
    
    async def _get_model_show(self, client: httpx.AsyncClient, model_id: str) -> Optional[dict]:
        """
        Get detailed model info from /api/show including capabilities.
        Returns dict with capabilities list (vision, tools, thinking, etc).
        Uses module-level cache with TTL to avoid repeated API calls.
        """
        # Check module-level cache first
        cached = _get_cached_model_show(model_id)
        if cached is not None:
            return cached
        
        try:
            resp = await client.post(
                f"{self.base_url}/api/show",
                headers=self.get_headers(),
                json={"model": model_id, "verbose": False},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                _set_cached_model_show(model_id, data)
                return data
            if resp.status_code == 404:
                return None
            # Other errors - might be auth issue, return None
            return None
        except Exception as e:
            logger.error(f"Ollama /api/show error for {model_id}: {e}")
            return None
    
    async def _fetch_capabilities(self, client: httpx.AsyncClient, model_names: list[str]) -> dict[str, Optional[list[str]]]:
        """
        Fetch capabilities for multiple models in parallel.
        Returns dict mapping model_name -> capabilities list (or None).
        """
        async def fetch_one(name: str) -> tuple[str, Optional[list[str]]]:
            model_show = await self._get_model_show(client, name)
            if not model_show:
                return name, None
            caps = model_show.get("capabilities", [])
            if not caps:
                return name, None
            supports = []
            cap_map = {
                "vision": "Vision",
                "tools": "Tools",
                "thinking": "Thinking",
                "completion": "Text",
                "embedding": "Embedding",
            }
            for c in caps:
                label = cap_map.get(c)
                if label:
                    supports.append(label)
            return name, supports if supports else None
        
        results = await asyncio.gather(*[fetch_one(name) for name in model_names])
        return {name: caps for name, caps in results}

    async def get_models(self, client: httpx.AsyncClient) -> list[dict]:
        """
        Get models from Ollama
        
        Logic:
        - /api/ps returns currently LOADED models (in VRAM)
        - /api/tags returns ALL available models (not necessarily loaded)
        - A model is "loaded" only if it appears in /api/ps
        - A model is "unloaded" if it appears in /api/tags but NOT in /api/ps
        - Cloud/remote models appear in /api/tags but NOT in /api/ps -> unloaded
        
        Note: Ollama doesn't expose activity status (active/idle), so activity_status is None
        """
        models = []
        
        # Get loaded models via /api/ps FIRST
        loaded_names = set()
        loaded_info = {}
        try:
            resp = await client.get(
                f"{self.base_url}/api/ps",
                headers=self.get_headers(),
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for rm in data.get("models", []):
                    name = rm.get("name", "")
                    loaded_names.add(name)
                    loaded_info[name] = {
                        "vram_size": rm.get("size_vram"),
                        "memory_size": rm.get("size"),  # total including system RAM
                        "context_length": rm.get("context_length")
                    }
        except Exception as e:
            logger.error(f"Ollama /api/ps error: {e}")
        
        # Get ALL models from /api/tags
        try:
            resp = await client.get(
                f"{self.base_url}/api/tags",
                headers=self.get_headers(),
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                # Collect all model names first for parallel capabilities fetch
                model_names = [m.get("name", "") for m in data.get("models", [])]
                
                # Fetch capabilities for all models in parallel (with cache)
                all_capabilities = await self._fetch_capabilities(client, model_names)
                
                for m in data.get("models", []):
                    name = m.get("name", "")
                    
                    # Check if cloud/remote model
                    is_cloud = bool(m.get("remote_model"))
                    
                    # Model is loaded only if it's in /api/ps
                    is_loaded = name in loaded_names
                    load_status = "loaded" if is_loaded else "unloaded"
                    
                    # Use loaded info if available
                    info = loaded_info.get(name, {})
                    
                    # Determine VRAM and model size
                    # VRAM from /api/ps (actual memory used) - only for loaded models
                    # model_size from /api/tags - for local models it's file size; for cloud models it's not meaningful
                    vram_size = info.get("vram_size") if is_loaded else None
                    model_size = None if is_cloud else m.get("size")
                    
                    # Get capabilities from cache/prefetched data
                    capabilities = all_capabilities.get(name)
                    if is_cloud and capabilities:
                        capabilities = list(capabilities) + ["Cloud"] if capabilities else ["Cloud"]
                    elif is_cloud and not capabilities:
                        capabilities = ["Cloud"]
                    
                    models.append({
                        "id": name,
                        "name": name,
                        "load_status": load_status,
                        "activity_status": None,  # Ollama doesn't expose this
                        "vram_size": vram_size,
                        "memory_size": info.get("memory_size") if is_loaded else None,
                        "model_size": model_size,
                        "context_window": info.get("context_length") or (
                            m.get("model", {}).get("context_length")
                            if isinstance(m.get("model"), dict) else None
                        ),
                        "supports": capabilities
                    })
        except Exception as e:
            logger.error(f"Ollama /api/tags error: {e}")
        
        return models
    
    async def load_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Load a model into memory by sending an empty prompt.
        Ollama loads the model when a request is made.
        """
        try:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                headers=self.get_headers(),
                json={"model": model_id, "prompt": "", "stream": False},
                timeout=30
            )
            if resp.status_code == 200:
                return {"success": True, "error": None}
            # Try to extract error message from response
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", f"HTTP {resp.status_code}")
            except:
                err_msg = f"HTTP {resp.status_code}"
            return {"success": False, "error": err_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def unload_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Unload a model from memory using POST /api/generate with empty prompt + keep_alive: 0
        """
        try:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                headers=self.get_headers(),
                json={"model": model_id, "prompt": "", "stream": False, "keep_alive": 0},
                timeout=30
            )
            if resp.status_code == 200:
                return {"success": True, "error": None}
            # Try to extract error message from response
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", f"HTTP {resp.status_code}")
            except:
                err_msg = f"HTTP {resp.status_code}"
            return {"success": False, "error": err_msg}
        except Exception as e:
            return {"success": False, "error": str(e)}
