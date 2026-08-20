from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from . import config
from .futurekawa_client import FuturekawaClient
from .mapping import (
    alert_signature,
    build_chatter_body,
    build_lot_vals,
    index_alertes,
    odoo_business_key,
    should_schedule_activity,
)
from .odoo_client import OdooAuthError, OdooClient, OdooConnectionError, OdooRpcError

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()

_derniere_alerte_notifiee: dict[str, str] = {}


@dataclass
class PaysSyncResult:
    pays: str
    status: str = "ok"
    lots_crees: int = 0
    lots_mis_a_jour: int = 0
    messages_postes: int = 0
    activites_planifiees: int = 0
    alertes_canal: int = 0
    erreurs: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = False
    product_id: int | None = None
    per_pays: list[PaysSyncResult] = field(default_factory=list)
    fatal_error: str | None = None


def run_sync(dry_run: bool | None = None, odoo_client_factory=None) -> SyncReport:
    """Point d'entrée public. Ne bloque jamais : si une synchro tourne déjà, retourne
    immédiatement un rapport avec fatal_error plutôt que d'attendre son tour."""
    started_at = datetime.now(timezone.utc)
    if not _sync_lock.acquire(blocking=False):
        logger.warning("Synchronisation déjà en cours — cycle ignoré")
        return SyncReport(
            started_at=started_at,
            finished_at=started_at,
            dry_run=config.ODOO_DRY_RUN if dry_run is None else dry_run,
            fatal_error="Une synchronisation est déjà en cours",
        )
    try:
        return _run_sync_locked(dry_run, odoo_client_factory)
    finally:
        _sync_lock.release()


def _default_odoo_client() -> OdooClient:
    return OdooClient(
        url=config.ODOO_URL,
        db=config.ODOO_DB,
        username=config.ODOO_USERNAME,
        api_key=config.ODOO_API_KEY,
        timeout=config.ODOO_TIMEOUT_SECONDS,
    )


def _run_sync_locked(dry_run, odoo_client_factory) -> SyncReport:
    effective_dry_run = config.ODOO_DRY_RUN if dry_run is None else dry_run
    report = SyncReport(started_at=datetime.now(timezone.utc), dry_run=effective_dry_run)
    build_client = odoo_client_factory or _default_odoo_client

    try:
        odoo = build_client()
        odoo.authenticate()
    except (OdooAuthError, OdooConnectionError, OdooRpcError) as exc:
        logger.error("Synchronisation annulée : connexion Odoo impossible : %s", exc)
        report.fatal_error = f"Connexion Odoo impossible : {exc}"
        report.finished_at = datetime.now(timezone.utc)
        return report

    try:
        produits = odoo.search_read(
            "product.product",
            [["default_code", "=", config.ODOO_DEFAULT_PRODUCT_DEFAULT_CODE]],
            ["id"],
            limit=1,
        )
    except (OdooConnectionError, OdooRpcError) as exc:
        logger.error("Synchronisation annulée : recherche du produit impossible : %s", exc)
        report.fatal_error = f"Recherche du produit Odoo impossible : {exc}"
        report.finished_at = datetime.now(timezone.utc)
        return report

    if not produits:
        message = (
            f"Aucun produit Odoo avec default_code='{config.ODOO_DEFAULT_PRODUCT_DEFAULT_CODE}' "
            "— créez-le avant la première synchronisation."
        )
        logger.error(message)
        report.fatal_error = message
        report.finished_at = datetime.now(timezone.utc)
        return report

    product_id = produits[0]["id"]
    report.product_id = product_id

    activity_user_id = None
    if config.ODOO_ACTIVITY_USER_LOGIN:
        try:
            activity_user_id = odoo.resolve_user_id(config.ODOO_ACTIVITY_USER_LOGIN)
            if activity_user_id is None:
                logger.warning(
                    "ODOO_ACTIVITY_USER_LOGIN='%s' introuvable — activités planifiées "
                    "sans assignation explicite",
                    config.ODOO_ACTIVITY_USER_LOGIN,
                )
        except (OdooConnectionError, OdooRpcError) as exc:
            logger.warning("Résolution de ODOO_ACTIVITY_USER_LOGIN impossible : %s", exc)

    fk_client = FuturekawaClient(
        config.BACKEND_FUTUREKAWA_URL, config.BACKEND_FUTUREKAWA_TIMEOUT_SECONDS
    )
    try:
        pays_liste = fk_client.fetch_pays()
    except httpx.HTTPError as exc:
        logger.error("Synchronisation annulée : backend_futurekawa injoignable : %s", exc)
        report.fatal_error = f"backend_futurekawa injoignable : {exc}"
        report.finished_at = datetime.now(timezone.utc)
        return report

    for entry in pays_liste:
        result = _sync_pays(fk_client, odoo, entry["nom"], product_id, effective_dry_run, activity_user_id)
        report.per_pays.append(result)

    report.finished_at = datetime.now(timezone.utc)
    return report


def _sync_pays(fk_client, odoo, pays, product_id, dry_run, activity_user_id) -> PaysSyncResult:
    result = PaysSyncResult(pays=pays)
    try:
        lots = fk_client.fetch_lots(pays)
        alertes = fk_client.fetch_alertes(pays)
    except httpx.HTTPError as exc:
        logger.warning("Pays '%s' indisponible côté backend_futurekawa : %s", pays, exc)
        result.status = "indisponible"
        return result

    idx = index_alertes(alertes)
    for lot in lots:
        try:
            _sync_lot(odoo, lot, idx, product_id, dry_run, activity_user_id, result)
        except (OdooConnectionError, OdooRpcError) as exc:
            message = f"Lot {lot.get('id')} ({pays}) : {exc}"
            logger.error(message)
            result.erreurs.append(message)
    return result


def _sync_lot(odoo, lot, idx, product_id, dry_run, activity_user_id, result: PaysSyncResult) -> None:
    key = odoo_business_key(lot["pays"], lot["id"])
    vals = build_lot_vals(lot, product_id, idx)

    existing = odoo.search_read("stock.lot", [["x_futurekawa_lot_id", "=", key]], ["id"], limit=1)

    if dry_run:
        logger.info("[DRY-RUN] %s le lot Odoo %s", "mettrait à jour" if existing else "créerait", key)
        result.lots_mis_a_jour += 1 if existing else 0
        result.lots_crees += 0 if existing else 1
        odoo_lot_id = existing[0]["id"] if existing else None
    elif existing:
        odoo_lot_id = existing[0]["id"]
        odoo.write("stock.lot", [odoo_lot_id], vals)
        result.lots_mis_a_jour += 1
    else:
        odoo_lot_id = odoo.create("stock.lot", vals)
        result.lots_crees += 1

    if odoo_lot_id is None:
        return 

    signature = alert_signature(lot, idx)
    if signature is None:
        _derniere_alerte_notifiee.pop(key, None)
        return
    if _derniere_alerte_notifiee.get(key) == signature:
        return

    if dry_run:
        logger.info("[DRY-RUN] noterait l'alerte sur le lot %s : %s", key, signature)
        return

    body = build_chatter_body(lot, idx)
    odoo.message_post("stock.lot", odoo_lot_id, body)
    result.messages_postes += 1

    try:
        odoo.post_to_channel(
            channel_name="Alertes",
            body=f"⚠️ <b>Alerte Qualité FutureKawa</b> (Lot <b>{key}</b>) :<br/>{signature}",
        )
        result.alertes_canal += 1
    except (OdooRpcError, OdooConnectionError) as exc:
        logger.warning("Impossible d'envoyer l'alerte au canal #Alertes pour %s : %s", key, exc)

    if should_schedule_activity(lot, idx):
        odoo.activity_schedule(
            "stock.lot",
            odoo_lot_id,
            summary=f"Alerte qualité FutureKawa — lot {key}",
            note=signature,
            user_id=activity_user_id,
        )
        result.activites_planifiees += 1

    _derniere_alerte_notifiee[key] = signature


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    report_result = run_sync()
    logger.info("Rapport de synchronisation : %s", report_result)