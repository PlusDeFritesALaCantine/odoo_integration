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
    champs_a_remonter,
    decoder_business_key,
    modifie_dans_odoo,
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
    lots_remontes: int = 0
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

    # Sens descendant d'abord : une correction faite dans Odoo doit être reprise
    # dans FutureKawa AVANT que la passe montante ne réécrive l'enregistrement,
    # sinon on écraserait la saisie humaine à chaque cycle.
    lots_par_id = {lot["id"]: lot for lot in lots}
    try:
        _sync_descendante(fk_client, odoo, pays, lots_par_id, dry_run, result)
    except (OdooConnectionError, OdooRpcError, httpx.HTTPError) as exc:
        message = f"Remontée Odoo -> FutureKawa ({pays}) : {exc}"
        logger.error(message)
        result.erreurs.append(message)

    idx = index_alertes(alertes)
    for lot in lots:
        try:
            _sync_lot(odoo, lot, idx, product_id, dry_run, activity_user_id, result)
        except (OdooConnectionError, OdooRpcError) as exc:
            message = f"Lot {lot.get('id')} ({pays}) : {exc}"
            logger.error(message)
            result.erreurs.append(message)
    return result


CHAMPS_LUS_POUR_REMONTEE = [
    "x_futurekawa_lot_id",
    "x_futurekawa_exploitation",
    "x_futurekawa_entrepot_id",
    "x_futurekawa_date_stockage",
    "x_futurekawa_derniere_sync_le",
    "write_date",
]


def _sync_descendante(fk_client, odoo, pays, lots_par_id, dry_run, result: PaysSyncResult) -> None:
    """Odoo -> FutureKawa : remonte les corrections saisies dans l'ERP.

    Ne remonte que les enregistrements dont write_date dépasse notre dernière
    écriture (cf. mapping.modifie_dans_odoo) et dont au moins un champ diffère
    réellement : sans ces deux filtres, chaque cycle enverrait un PATCH par lot.

    Les lots présents dans Odoo mais inconnus de FutureKawa sont ignorés : la
    création de lot reste du ressort de FutureKawa, Odoo n'est qu'un miroir
    enrichi. Les supprimer côté Odoo serait destructif, on préfère les laisser.
    """
    if not lots_par_id:
        return

    records = odoo.search_read(
        "stock.lot",
        [["x_futurekawa_pays", "=", pays]],
        CHAMPS_LUS_POUR_REMONTEE,
    )

    for record in records:
        decode = decoder_business_key(record.get("x_futurekawa_lot_id") or "")
        if decode is None:
            continue
        _, lot_id = decode
        lot = lots_par_id.get(lot_id)
        if lot is None:
            continue
        if not modifie_dans_odoo(record):
            continue

        ecart = champs_a_remonter(record, lot)
        if not ecart:
            continue

        if dry_run:
            logger.info("[dry-run] Remontée %s:%s -> %s", pays, lot_id, ecart)
            result.lots_remontes += 1
            continue

        fk_client.patch_lot(pays, lot_id, ecart)
        # La passe montante qui suit doit repartir des valeurs à jour, sinon elle
        # réécrirait immédiatement l'ancienne valeur dans Odoo.
        lot.update(ecart)
        result.lots_remontes += 1
        logger.info("Remontée Odoo -> FutureKawa : %s:%s %s", pays, lot_id, ecart)


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
    
    # 1. Si aucune alerte n'est présente sur le lot, réinitialiser la signature
    if signature is None:
        _derniere_alerte_notifiee.pop(key, None)
        logger.info("Aucune alerte active pour le lot %s", key)
        return

    # 2. Vérification en mémoire (pendant le même processus)
    if _derniere_alerte_notifiee.get(key) == signature:
        logger.info("Alerte déjà traitée en mémoire pour le lot %s", key)
        return

    if odoo_lot_id and not dry_run:
        messages = odoo.search_read(
            "mail.message",
            [
                ["model", "=", "stock.lot"],
                ["res_id", "=", odoo_lot_id],
                ["body", "ilike", signature],
            ],
            ["id"],
            limit=1,
        )
        if messages:
            _derniere_alerte_notifiee[key] = signature
            logger.info("Alerte '%s' déjà enregistrée sur Odoo pour le lot %s — passage ignoré.", signature, key)
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