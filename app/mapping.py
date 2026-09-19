"""Transformation pure des données FutureKawa (lots + alertes) vers des valeurs Odoo.

Aucune fonction ici ne fait d'I/O (HTTP, XML-RPC) : entièrement testable sans mock,
sur le même principe que app/services/alertes.py dans api_futurekawa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def odoo_business_key(pays: str, lot_id: str) -> str:
    """Clé métier unique tous pays confondus, utilisée comme name + x_futurekawa_lot_id.

    stock.lot impose une contrainte native unique(product_id, company_id, name) :
    comme tous les lots partagent le même produit "café vert", deux pays produisant
    chacun un lot d'id brut identique (ex. les deux commencent à "L1") entreraient en
    collision si `name` restait l'id brut. Préfixer par le pays l'évite.
    """
    return f"{pays}:{lot_id}"


@dataclass
class AlertIndex:
    """Index des alertes actives d'un pays, construit une fois par cycle de synchro."""

    expired_lot_ids: dict[str, str] = field(default_factory=dict)
    """lot_id -> raison (lot périmé, cf. api_futurekawa lots_problematiques)."""

    entrepot_alerts: dict[str, tuple[str, str, dict]] = field(default_factory=dict)
    """entrepot_id -> (raison, severite, mesure_dict), cf. mesures_hors_seuil."""


def index_alertes(alertes: dict) -> AlertIndex:
    """Construit un AlertIndex à partir de la réponse AlertesResponse de l'API pays."""
    idx = AlertIndex()
    for item in alertes.get("lots_problematiques", []):
        idx.expired_lot_ids[item["lot"]["id"]] = item["raison"]
    for item in alertes.get("mesures_hors_seuil", []):
        mesure = item["mesure"]
        idx.entrepot_alerts[mesure["entrepot_id"]] = (item["raison"], item["severite"], mesure)
    return idx


def compute_statut(lot: dict, idx: AlertIndex) -> str:
    """"en_alerte" n'existe pas comme statut brut côté api_futurekawa (juste conforme/perime) :
    il est dérivé ici de la présence du lot/entrepôt dans les alertes actives."""
    if lot["id"] in idx.expired_lot_ids:
        return "perime"
    if lot["entrepot_id"] in idx.entrepot_alerts:
        return "en_alerte"
    return "conforme"


def _to_odoo_datetime(iso_timestamp: str) -> str:
    """Odoo XML-RPC attend "YYYY-MM-DD HH:MM:SS" en UTC naïf (pas de suffixe Z/offset)."""
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_lot_vals(lot: dict, product_id: int, idx: AlertIndex) -> dict:
    """Valeurs stock.lot à créer/écrire pour ce lot.

    Les champs "dernière température/humidité/mesure" ne sont renseignés que si
    l'entrepôt du lot a une mesure hors seuil active : le module ne réplique pas
    l'historique complet des relevés IoT (hors périmètre de cette synchro), seulement
    l'état déclenchant une alerte.
    """
    vals = {
        "name": odoo_business_key(lot["pays"], lot["id"]),
        "product_id": product_id,
        "x_futurekawa_lot_id": odoo_business_key(lot["pays"], lot["id"]),
        "x_futurekawa_pays": lot["pays"],
        "x_futurekawa_exploitation": lot["exploitation"],
        "x_futurekawa_entrepot_id": lot["entrepot_id"],
        "x_futurekawa_date_stockage": lot["date_stockage"],
        "x_futurekawa_statut": compute_statut(lot, idx),
        "x_futurekawa_derniere_sync_le": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    mesure_alert = idx.entrepot_alerts.get(lot["entrepot_id"])
    if mesure_alert:
        _, _, mesure = mesure_alert
        vals["x_futurekawa_derniere_temperature"] = mesure["temperature"]
        vals["x_futurekawa_derniere_humidite"] = mesure["humidity"]
        vals["x_futurekawa_derniere_mesure_le"] = _to_odoo_datetime(mesure["timestamp"])
    return vals


def alert_signature(lot: dict, idx: AlertIndex) -> str | None:
    """Texte de l'alerte active sur ce lot, ou None si aucune.

    sync.py compare cette signature à la dernière notifiée pour ce lot afin de ne
    reposter une note/activité que si l'alerte a changé — sans quoi chaque cycle de
    synchro (toutes les SYNC_INTERVAL_SECONDS) republierait le même message tant que
    l'alerte reste active.
    """
    raisons = []
    if lot["id"] in idx.expired_lot_ids:
        raisons.append(idx.expired_lot_ids[lot["id"]])
    mesure_alert = idx.entrepot_alerts.get(lot["entrepot_id"])
    if mesure_alert:
        raisons.append(mesure_alert[0])
    return " / ".join(raisons) if raisons else None


def build_chatter_body(lot: dict, idx: AlertIndex) -> str | None:
    signature = alert_signature(lot, idx)
    if signature is None:
        return None
    return f"Alerte FutureKawa : {signature}"


def should_schedule_activity(lot: dict, idx: AlertIndex) -> bool:
    """Activité planifiée seulement pour les cas les plus sévères : lot périmé, ou
    mesure classée "critique" (cf. TIERS_ALERTE/MULTIPLICATEUR_CRITIQUE dans
    api_futurekawa/app/services/alertes.py). Le tier "bas" reste une simple note."""
    if lot["id"] in idx.expired_lot_ids:
        return True
    mesure_alert = idx.entrepot_alerts.get(lot["entrepot_id"])
    return bool(mesure_alert and mesure_alert[1] == "critique")


# --- Sens descendant : Odoo -> FutureKawa --------------------------------
#
# La synchronisation était unidirectionnelle : une correction faite dans Odoo
# (mauvaise exploitation saisie, lot déplacé d'entrepôt) était écrasée au cycle
# suivant par la valeur FutureKawa. Les fonctions ci-dessous décident, pour un
# enregistrement stock.lot donné, s'il a été modifié par un humain depuis notre
# dernière écriture, et quels champs remonter.
#
# Seuls trois champs sont remontés. Les autres sont soit dérivés (statut,
# relevés capteur), soit identifiants (pays, lot_id) : les laisser modifiables
# depuis Odoo casserait la correspondance entre les deux systèmes.

CHAMPS_REMONTES = {
    "x_futurekawa_exploitation": "exploitation",
    "x_futurekawa_entrepot_id": "entrepot_id",
    "x_futurekawa_date_stockage": "date_stockage",
}

# Notre propre écriture met à jour write_date : sans marge, chaque lot poussé
# serait aussitôt considéré comme modifié dans Odoo.
TOLERANCE_ECRITURE_SECONDES = 5


def decoder_business_key(key: str) -> tuple[str, str] | None:
    """'bresil:LOT-BR-001' -> ('bresil', 'LOT-BR-001'). None si la clé est inexploitable."""
    pays, separateur, lot_id = (key or "").partition(":")
    if not separateur or not pays or not lot_id:
        return None
    return pays, lot_id


def _parse_odoo_datetime(valeur) -> datetime | None:
    """Odoo renvoie 'YYYY-MM-DD HH:MM:SS' en UTC naïf, ou False si le champ est vide."""
    if not valeur:
        return None
    try:
        return datetime.strptime(str(valeur), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def modifie_dans_odoo(record: dict, tolerance_secondes: int = TOLERANCE_ECRITURE_SECONDES) -> bool:
    """Vrai si l'enregistrement a été touché après notre dernière écriture.

    C'est l'arbitrage de conflit : tant que write_date reste aligné sur
    x_futurekawa_derniere_sync_le, la dernière modification vient de nous et
    FutureKawa fait foi. Dès qu'un humain édite dans Odoo, write_date prend de
    l'avance et c'est Odoo qui fait foi pour les champs remontés.
    """
    write_date = _parse_odoo_datetime(record.get("write_date"))
    if write_date is None:
        return False
    derniere_sync = _parse_odoo_datetime(record.get("x_futurekawa_derniere_sync_le"))
    if derniere_sync is None:
        # Jamais écrit par nous : l'enregistrement a été créé ou modifié à la main.
        return True
    return write_date > derniere_sync + timedelta(seconds=tolerance_secondes)


def champs_a_remonter(record: dict, lot: dict) -> dict:
    """Champs Odoo qui diffèrent du lot FutureKawa, nommés comme l'API FutureKawa.

    Retourne un dict vide s'il n'y a rien à remonter — l'appelant ne doit alors
    émettre aucun PATCH.
    """
    ecart = {}
    for champ_odoo, champ_fk in CHAMPS_REMONTES.items():
        valeur = record.get(champ_odoo)
        if valeur is False or valeur is None or valeur == "":
            continue  # champ vidé dans Odoo : on ne propage pas un effacement
        if champ_fk == "date_stockage":
            valeur = str(valeur)[:10]
        if str(valeur) != str(lot.get(champ_fk)):
            ecart[champ_fk] = valeur
    return ecart
