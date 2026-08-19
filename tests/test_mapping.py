from app.mapping import (
    alert_signature,
    build_chatter_body,
    build_lot_vals,
    compute_statut,
    index_alertes,
    odoo_business_key,
    should_schedule_activity,
)

LOT_BASE = {
    "id": "L1",
    "pays": "bresil",
    "exploitation": "Fazenda X",
    "entrepot_id": "e1",
    "date_stockage": "2026-05-19",
    "statut": "conforme",
}

ALERTES_VIDES = {"lots_problematiques": [], "mesures_hors_seuil": []}


def alertes_perime(lot):
    return {
        "lots_problematiques": [{"lot": lot, "raison": "Lot périmé : stocké depuis 400 jours"}],
        "mesures_hors_seuil": [],
    }


def alertes_mesure(entrepot_id, severite, temperature=35.0, humidity=50.0):
    return {
        "lots_problematiques": [],
        "mesures_hors_seuil": [
            {
                "mesure": {
                    "id": "M1",
                    "entrepot_id": entrepot_id,
                    "temperature": temperature,
                    "humidity": humidity,
                    "timestamp": "2026-07-06T14:32:05Z",
                },
                "raison": "température trop élevée : 35.0°C (seuil 26.0–32.0°C)",
                "severite": severite,
            }
        ],
    }


def test_odoo_business_key_unique_across_countries_same_raw_id():
    assert odoo_business_key("bresil", "L1") != odoo_business_key("equateur", "L1")
    assert odoo_business_key("bresil", "L1") == "bresil:L1"


def test_lot_conforme_sans_alerte():
    idx = index_alertes(ALERTES_VIDES)
    assert compute_statut(LOT_BASE, idx) == "conforme"
    assert build_chatter_body(LOT_BASE, idx) is None
    assert should_schedule_activity(LOT_BASE, idx) is False

    vals = build_lot_vals(LOT_BASE, product_id=1, idx=idx)
    assert vals["name"] == "bresil:L1"
    assert vals["x_futurekawa_lot_id"] == "bresil:L1"
    assert vals["x_futurekawa_statut"] == "conforme"
    assert "x_futurekawa_derniere_temperature" not in vals


def test_lot_perime():
    idx = index_alertes(alertes_perime(LOT_BASE))
    assert compute_statut(LOT_BASE, idx) == "perime"
    assert "périmé" in build_chatter_body(LOT_BASE, idx)
    assert should_schedule_activity(LOT_BASE, idx) is True


def test_mesure_hors_seuil_bas_est_en_alerte_sans_activite():
    idx = index_alertes(alertes_mesure("e1", "bas"))
    assert compute_statut(LOT_BASE, idx) == "en_alerte"
    assert build_chatter_body(LOT_BASE, idx) is not None
    assert should_schedule_activity(LOT_BASE, idx) is False

    vals = build_lot_vals(LOT_BASE, product_id=1, idx=idx)
    assert vals["x_futurekawa_derniere_temperature"] == 35.0
    assert vals["x_futurekawa_derniere_humidite"] == 50.0
    assert vals["x_futurekawa_derniere_mesure_le"] == "2026-07-06 14:32:05"


def test_mesure_hors_seuil_critique_declenche_activite():
    idx = index_alertes(alertes_mesure("e1", "critique"))
    assert should_schedule_activity(LOT_BASE, idx) is True


def test_alerte_sur_autre_entrepot_ne_concerne_pas_ce_lot():
    idx = index_alertes(alertes_mesure("autre-entrepot", "critique"))
    assert compute_statut(LOT_BASE, idx) == "conforme"
    assert should_schedule_activity(LOT_BASE, idx) is False


def test_alert_signature_none_quand_conforme():
    idx = index_alertes(ALERTES_VIDES)
    assert alert_signature(LOT_BASE, idx) is None


def test_alert_signature_combine_peremption_et_mesure():
    alertes = alertes_perime(LOT_BASE)
    alertes["mesures_hors_seuil"] = alertes_mesure("e1", "critique")["mesures_hors_seuil"]
    idx = index_alertes(alertes)
    signature = alert_signature(LOT_BASE, idx)
    assert "périmé" in signature
    assert "température" in signature