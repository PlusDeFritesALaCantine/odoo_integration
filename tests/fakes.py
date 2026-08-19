"""Doubles de test pour l'API XML-RPC d'Odoo.

respx (utilisé ailleurs dans le projet pour mocker httpx) ne s'applique pas ici :
xmlrpc.client parle HTTP en interne mais via des mécanismes non interceptables par
respx. On fournit donc de faux objets `common`/`object` injectables dans OdooClient,
qui reproduisent juste assez la sémantique de execute_kw pour les besoins des tests
(recherche par égalité, create/write/message_post/activity_schedule en mémoire).
"""

from __future__ import annotations

import itertools
import xmlrpc.client


class FakeOdooCommon:
    def __init__(self, uid: int = 7, valid_db=None, valid_login=None, valid_api_key=None):
        self.uid = uid
        self.valid_db = valid_db
        self.valid_login = valid_login
        self.valid_api_key = valid_api_key
        self.authenticate_calls: list[tuple] = []

    def authenticate(self, db, login, api_key, context):
        self.authenticate_calls.append((db, login, api_key, context))
        if self.valid_db is not None and (db, login, api_key) != (
            self.valid_db,
            self.valid_login,
            self.valid_api_key,
        ):
            return False
        return self.uid


class RaisingCommon:
    """Simule une instance Odoo injoignable dès l'authentification."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def authenticate(self, db, login, api_key, context):
        raise self._exc


class FakeOdooObject:
    def __init__(self):
        self._store: dict[str, dict[int, dict]] = {}
        self._ids = itertools.count(1)
        self.calls: list[tuple] = []
        self.messages: list[tuple] = []
        self.activities: list[tuple] = []
        self._faults: dict[tuple[str, str], xmlrpc.client.Fault] = {}

    def seed(self, model: str, id_: int, vals: dict) -> None:
        self._store.setdefault(model, {})[id_] = dict(vals)

    def fail_next(self, model: str, method: str, fault: xmlrpc.client.Fault) -> None:
        self._faults[(model, method)] = fault

    def execute_kw(self, db, uid, api_key, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method, args, kwargs))

        fault = self._faults.pop((model, method), None)
        if fault is not None:
            raise fault

        table = self._store.setdefault(model, {})

        if method == "search_read":
            domain = args[0] if args else []
            fields = kwargs.get("fields")
            limit = kwargs.get("limit")
            if model == "ir.model" and not table:
                target_model = domain[0][2] if domain and len(domain[0]) == 3 else "stock.lot"
                return [{"id": 1, "model": target_model}]
            if model == "mail.activity.type" and not table:
                return [{"id": 1}]

            results = []
            for id_, vals in table.items():
                if _match_domain(vals, domain):
                    champs = [f for f in (fields or vals.keys()) if f != "id"]
                    row = {f: vals.get(f) for f in champs}
                    row["id"] = id_
                    results.append(row)
            return results[:limit] if limit is not None else results

        if method == "create":
            (vals,) = args
            new_id = next(self._ids)
            table[new_id] = dict(vals)
            if model == "mail.activity":
                res_model = vals.get("res_model", "stock.lot")
                res_id = vals.get("res_id")
                summary = vals.get("summary")
                note = vals.get("note")
                user_id = vals.get("user_id")
                self.activities.append((res_model, res_id, summary, note, user_id))

            return new_id

        if method == "write":
            ids, vals = args
            for id_ in ids:
                table.setdefault(id_, {}).update(vals)
            return True

        if method == "message_post":
            (res_ids,) = args
            self.messages.append((model, res_ids[0], kwargs.get("body")))
            return len(self.messages)

        if method == "x_futurekawa_activity_schedule":
            (res_ids,) = args
            self.activities.append(
                (model, res_ids[0], kwargs.get("summary"), kwargs.get("note"), kwargs.get("user_id"))
            )
            return [len(self.activities)]

        raise NotImplementedError(f"FakeOdooObject ne gère pas la méthode '{method}'")


def _match_domain(vals: dict, domain: list) -> bool:
    """Sous-ensemble minimal : clauses ["champ","=",valeur] combinées en ET (suffisant
    ici car sync.py ne fait que des recherches par égalité simple)."""
    for item in domain:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        champ, op, valeur = item
        if op == "in":
            if vals.get(champ) not in valeur:
                return False
        elif op == "=":
            if vals.get(champ) != valeur:
                return False
        else:
            raise NotImplementedError(f"Opérateur de domaine non supporté par le fake : {op}")
    return True