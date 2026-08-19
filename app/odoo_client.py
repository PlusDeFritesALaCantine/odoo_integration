from __future__ import annotations

import logging
import xmlrpc.client
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class OdooAuthError(Exception):
    """Identifiants refusés par Odoo (uid non obtenu)."""


class OdooConnectionError(Exception):
    """Instance Odoo injoignable (réseau, DNS, timeout)."""


class OdooRpcError(Exception):
    """Odoo a rejeté l'appel (validation, contrainte SQL, droits) — xmlrpc.client.Fault."""


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _build_transport(url: str, timeout: float):
    scheme = urlsplit(url).scheme
    transport_cls = _TimeoutSafeTransport if scheme == "https" else _TimeoutTransport
    return transport_cls(timeout=timeout)


class OdooClient:
    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        api_key: str,
        timeout: float = 10.0,
        common_proxy=None,
        object_proxy=None,
    ):
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.api_key = api_key
        self.timeout = timeout
        self._common = common_proxy or xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", transport=_build_transport(self.url, timeout)
        )
        self._object = object_proxy or xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object", transport=_build_transport(self.url, timeout)
        )
        self.uid: int | None = None

    def authenticate(self) -> int:
        try:
            uid = self._common.authenticate(self.db, self.username, self.api_key, {})
        except xmlrpc.client.Fault as exc:
            raise OdooRpcError(f"Authentification rejetée par Odoo : {exc.faultString}") from exc
        except (OSError, TimeoutError) as exc:
            raise OdooConnectionError(f"Connexion à Odoo impossible ({self.url}) : {exc}") from exc
        if not uid:
            raise OdooAuthError(f"Identifiants Odoo invalides pour {self.username}@{self.db}")
        self.uid = uid
        return uid

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        if self.uid is None:
            self.authenticate()
        try:
            return self._object.execute_kw(
                self.db, self.uid, self.api_key, model, method, args, kwargs or {}
            )
        except xmlrpc.client.Fault as exc:
            raise OdooRpcError(f"Odoo a rejeté {model}.{method} : {exc.faultString}") from exc
        except (OSError, TimeoutError) as exc:
            raise OdooConnectionError(
                f"Connexion à Odoo perdue pendant {model}.{method} : {exc}"
            ) from exc

    def search_read(
        self, model: str, domain: list, fields: list[str], limit: int | None = None
    ) -> list[dict]:
        kwargs: dict = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        return self._execute(model, "search_read", [domain], kwargs)

    def create(self, model: str, vals: dict) -> int:
        return self._execute(model, "create", [vals])

    def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return self._execute(model, "write", [ids, vals])

    def message_post(self, model: str, res_id: int, body: str) -> int:
        return self._execute(
            model,
            "message_post",
            [[res_id]],
            {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
        )

    def activity_schedule(
        self,
        model: str,
        res_id: int,
        summary: str,
        note: str,
        user_id: int | None = None,
        act_type_xmlid: str = "mail.mail_activity_data_todo",
    ) -> int:
        """Planifie une activité en créant un enregistrement natif 'mail.activity'."""
        model_records = self.search_read("ir.model", [["model", "=", model]], ["id"], limit=1)
        if not model_records:
            raise OdooRpcError(f"Modèle Odoo introuvable : {model}")
        res_model_id = model_records[0]["id"]
        activity_type_records = self.search_read(
            "mail.activity.type",
            [["res_model", "in", [model, False]]],
            ["id"],
            limit=1,
        )
        activity_type_id = activity_type_records[0]["id"] if activity_type_records else 1
        activity_vals: dict = {
            "res_model_id": res_model_id,
            "res_id": res_id,
            "activity_type_id": activity_type_id,
            "summary": summary,
            "note": note,
        }
        if user_id is not None:
            activity_vals["user_id"] = user_id

        return self.create("mail.activity", activity_vals)

    def resolve_user_id(self, login: str) -> int | None:
        results = self.search_read("res.users", [["login", "=", login]], ["id"], limit=1)
        return results[0]["id"] if results else None

    def post_to_channel(self, channel_name: str, body: str) -> int:
        """Envoie un message dans un canal de discussion Odoo/Discuss (ex: '# Alertes')."""
        # 1. Tenter la recherche avec le modèle Odoo moderne (discuss.channel), puis l'ancien (mail.channel)
        channel_model = "discuss.channel"
        channels = []
        try:
            channels = self.search_read(channel_model, [["name", "=", channel_name]], ["id"], limit=1)
        except OdooRpcError:
            channel_model = "mail.channel"
            channels = self.search_read(channel_model, [["name", "=", channel_name]], ["id"], limit=1)

        if not channels:
            raise OdooRpcError(f"Canal de discussion introuvable : #{channel_name}")

        channel_id = channels[0]["id"]

        return self.message_post(channel_model, channel_id, body)