"""
Base adapter for AI inference servers
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """Base class for all server adapters"""
    
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
    
    def get_headers(self) -> dict:
        """Get headers for API requests"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    @abstractmethod
    async def check_health(self, client: httpx.AsyncClient) -> dict:
        """
        Check if the server is healthy/online
        Returns dict with 'status' (online/offline/error) and optional 'error', 'total_memory', 'used_memory'
        """
        raise NotImplementedError
    
    @abstractmethod
    async def get_models(self, client: httpx.AsyncClient) -> list[dict]:
        """
        Get list of models from the server
        Returns list of dicts with: id, name, status (loaded/unloaded/running), memory, context_window
        """
        raise NotImplementedError
    
    async def load_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Load a model into memory
        Returns dict with 'success' (bool) and optional 'error' (str)
        """
        raise NotImplementedError
    
    async def unload_model(self, client: httpx.AsyncClient, model_id: str) -> dict:
        """
        Unload a model from memory
        Returns dict with 'success' (bool) and optional 'error' (str)
        """
        raise NotImplementedError