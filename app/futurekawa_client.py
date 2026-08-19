from __future__ import annotations
import httpx

class FuturekawaClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url
        self.timeout = timeout

    def _get(self, path: str) -> object:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()

    def fetch_pays(self) -> list[dict]:
        return self._get("/pays")

    def fetch_lots(self, pays: str) -> list[dict]:
        return self._get(f"/pays/{pays}/lots")

    def fetch_alertes(self, pays: str) -> dict:
        return self._get(f"/pays/{pays}/alertes")