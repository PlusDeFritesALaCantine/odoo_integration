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

    def patch_lot(self, pays: str, lot_id: str, champs: dict) -> dict:
        """Remonte vers FutureKawa une correction faite dans Odoo.

        PATCH et non PUT : l'API pays fusionne les champs fournis (exclude_unset)
        au lieu de remplacer le lot. Envoyer un PUT partiel viderait les champs
        absents de la charge utile.
        """
        with httpx.Client(timeout=self.timeout) as client:
            response = client.patch(
                f"{self.base_url}/pays/{pays}/lots/{lot_id}", json=champs
            )
            response.raise_for_status()
            return response.json()