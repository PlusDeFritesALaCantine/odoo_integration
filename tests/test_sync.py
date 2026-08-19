import threading
import xmlrpc.client

from app import config
import httpx
import pytest
import respx

from app import sync
from app.odoo_client import OdooClient
from tests.fakes import FakeOdooCommon, FakeOdooObject

BACKEND_URL = "http://backend-test:8002"
PRODUCT_CODE = "CAFE-VERT"

LOT_BRESIL = {
    "id": "L1",
    "pays": "bresil",
    "exploitation": "Fazenda X",
    "entrepot_id": "e1",
    "date_stockage": "2026-05-19",
    "statut": "conforme",
}
LOT_EQUATEUR = {
    "id": "L1", 
    "pays": "equateur",
    "exploitation": "Finca Y",
    "entrepot_id": "e2",
    "date_stockage": "2026-05-20",
    "statut": "conforme",
}
ALERTES_VIDES = {"lots_problematiques": [], "mesures_hors_seuil": []}


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr(config, "BACKEND_FUTUREKAWA_URL", BACKEND_URL)
    monkeypatch.setattr(config, "ODOO_DEFAULT_PRODUCT_DEFAULT_CODE", PRODUCT_CODE)
    monkeypatch.setattr(config, "ODOO_ACTIVITY_USER_LOGIN", "")
    sync._derniere_alerte_notifiee.clear()
    yield
    sync._derniere_alerte_notifiee.clear()


def make_factory(obj: FakeOdooObject, common: FakeOdooCommon | None = None):
    common = common or FakeOdooCommon(uid=1)
    obj.seed("product.product", 100, {"default_code": PRODUCT_CODE})
    obj.seed("discuss.channel", 1, {"name": "Alertes"})

    def factory():
        return OdooClient(
            url="https://odoo.example.test",
            db="futurekawa",
            username="integration@futurekawa.local",
            api_key="fake-key",
            common_proxy=common,
            object_proxy=obj,
        )

    return factory


def mock_pays(pays_list, lots_by_pays, alertes_by_pays=None):
    alertes_by_pays = alertes_by_pays or {p: ALERTES_VIDES for p in pays_list}
    respx.get(f"{BACKEND_URL}/pays").mock(
        return_value=httpx.Response(200, json=[{"nom": p, "url": "x"} for p in pays_list])
    )
    for p in pays_list:
        respx.get(f"{BACKEND_URL}/pays/{p}/lots").mock(
            return_value=httpx.Response(200, json=lots_by_pays.get(p, []))
        )
        respx.get(f"{BACKEND_URL}/pays/{p}/alertes").mock(
            return_value=httpx.Response(200, json=alertes_by_pays.get(p, ALERTES_VIDES))
        )


@respx.mock
def test_premiere_synchro_cree_les_lots():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    mock_pays(["bresil", "equateur"], {"bresil": [LOT_BRESIL], "equateur": [LOT_EQUATEUR]})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert report.fatal_error is None
    assert report.product_id == 100
    resultats = {r.pays: r for r in report.per_pays}
    assert resultats["bresil"].lots_crees == 1
    assert resultats["equateur"].lots_crees == 1
    assert len(obj._store["stock.lot"]) == 2


@respx.mock
def test_deuxieme_synchro_met_a_jour_au_lieu_de_creer():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]})

    premier = sync.run_sync(dry_run=False, odoo_client_factory=factory)
    second = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert premier.per_pays[0].lots_crees == 1
    assert second.per_pays[0].lots_crees == 0
    assert second.per_pays[0].lots_mis_a_jour == 1
    assert len(obj._store["stock.lot"]) == 1


@respx.mock
def test_dry_run_ne_modifie_jamais_odoo():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]})

    report = sync.run_sync(dry_run=True, odoo_client_factory=factory)

    assert report.dry_run is True
    assert report.per_pays[0].lots_crees == 1
    assert obj._store.get("stock.lot", {}) == {}
    assert obj.messages == []
    assert obj.activities == []


@respx.mock
def test_pays_indisponible_n_empeche_pas_les_autres():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    respx.get(f"{BACKEND_URL}/pays").mock(
        return_value=httpx.Response(200, json=[{"nom": "bresil", "url": "x"}, {"nom": "equateur", "url": "y"}])
    )
    respx.get(f"{BACKEND_URL}/pays/bresil/lots").mock(return_value=httpx.Response(200, json=[LOT_BRESIL]))
    respx.get(f"{BACKEND_URL}/pays/bresil/alertes").mock(return_value=httpx.Response(200, json=ALERTES_VIDES))
    respx.get(f"{BACKEND_URL}/pays/equateur/lots").mock(side_effect=httpx.ConnectError("offline"))
    respx.get(f"{BACKEND_URL}/pays/equateur/alertes").mock(side_effect=httpx.ConnectError("offline"))

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    resultats = {r.pays: r for r in report.per_pays}
    assert resultats["bresil"].status == "ok"
    assert resultats["bresil"].lots_crees == 1
    assert resultats["equateur"].status == "indisponible"


@respx.mock
def test_alerte_critique_planifie_une_activite_bas_seulement_une_note():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    lot_critique = {**LOT_BRESIL, "id": "L-critique", "entrepot_id": "e-critique"}
    lot_bas = {**LOT_BRESIL, "id": "L-bas", "entrepot_id": "e-bas"}
    alertes = {
        "lots_problematiques": [],
        "mesures_hors_seuil": [
            {
                "mesure": {"id": "M1", "entrepot_id": "e-critique", "temperature": 40.0, "humidity": 50.0, "timestamp": "2026-07-06T14:32:05Z"},
                "raison": "température très élevée",
                "severite": "critique",
            },
            {
                "mesure": {"id": "M2", "entrepot_id": "e-bas", "temperature": 33.0, "humidity": 50.0, "timestamp": "2026-07-06T14:32:05Z"},
                "raison": "température un peu élevée",
                "severite": "bas",
            },
        ],
    }
    mock_pays(["bresil"], {"bresil": [lot_critique, lot_bas]}, {"bresil": alertes})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert report.per_pays[0].messages_postes == 2
    assert report.per_pays[0].activites_planifiees == 1
    assert report.per_pays[0].alertes_canal == 2
    assert len(obj.activities) == 1


@respx.mock
def test_alerte_publiee_sur_canal_discuss():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    alertes = {
        "lots_problematiques": [{"lot": LOT_BRESIL, "raison": "Lot périmé : stocké depuis 400 jours"}],
        "mesures_hors_seuil": [],
    }
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]}, {"bresil": alertes})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert report.per_pays[0].alertes_canal == 1
    canal_messages = [m for m in obj.messages if m[0] == "discuss.channel"]
    assert len(canal_messages) == 1
    assert "Alerte Qualité FutureKawa" in canal_messages[0][2]


@respx.mock
def test_dry_run_ne_publie_pas_sur_canal():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    alertes = {
        "lots_problematiques": [{"lot": LOT_BRESIL, "raison": "Lot périmé : stocké depuis 400 jours"}],
        "mesures_hors_seuil": [],
    }
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]}, {"bresil": alertes})

    report = sync.run_sync(dry_run=True, odoo_client_factory=factory)

    assert report.per_pays[0].alertes_canal == 0
    assert obj.messages == []


@respx.mock
def test_erreur_canal_n_empeche_pas_la_synchro_du_lot():
    obj = FakeOdooObject()
    common = FakeOdooCommon(uid=1)
    obj.seed("product.product", 100, {"default_code": PRODUCT_CODE})

    def factory():
        return OdooClient(
            url="https://odoo.example.test",
            db="futurekawa",
            username="integration@futurekawa.local",
            api_key="fake-key",
            common_proxy=common,
            object_proxy=obj,
        )

    alertes = {
        "lots_problematiques": [{"lot": LOT_BRESIL, "raison": "Lot périmé : stocké depuis 400 jours"}],
        "mesures_hors_seuil": [],
    }
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]}, {"bresil": alertes})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert report.per_pays[0].lots_crees == 1
    assert report.per_pays[0].messages_postes == 1
    assert report.per_pays[0].alertes_canal == 0
    assert report.per_pays[0].erreurs == []


@respx.mock
def test_meme_alerte_non_repostee_au_cycle_suivant():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    alertes = {
        "lots_problematiques": [{"lot": LOT_BRESIL, "raison": "Lot périmé : stocké depuis 400 jours"}],
        "mesures_hors_seuil": [],
    }
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]}, {"bresil": alertes})

    premier = sync.run_sync(dry_run=False, odoo_client_factory=factory)
    second = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert premier.per_pays[0].messages_postes == 1
    assert premier.per_pays[0].alertes_canal == 1
    assert second.per_pays[0].messages_postes == 0
    assert second.per_pays[0].alertes_canal == 0


@respx.mock
def test_echec_sur_un_lot_n_arrete_pas_les_autres():
    obj = FakeOdooObject()
    factory = make_factory(obj)
    obj.fail_next("stock.lot", "create", xmlrpc.client.Fault(1, "contrainte violée"))
    lot_ok = {**LOT_BRESIL, "id": "L-ok", "entrepot_id": "e-ok"}
    mock_pays(["bresil"], {"bresil": [LOT_BRESIL, lot_ok]})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    resultat = report.per_pays[0]
    assert len(resultat.erreurs) == 1
    assert resultat.lots_crees == 1


@respx.mock
def test_produit_absent_est_une_erreur_fatale():
    obj = FakeOdooObject()
    common = FakeOdooCommon(uid=1)

    def factory():
        return OdooClient(
            url="https://odoo.example.test",
            db="futurekawa",
            username="u",
            api_key="k",
            common_proxy=common,
            object_proxy=obj,
        )

    mock_pays(["bresil"], {"bresil": [LOT_BRESIL]})

    report = sync.run_sync(dry_run=False, odoo_client_factory=factory)

    assert report.fatal_error is not None
    assert report.per_pays == []


def test_synchro_deja_en_cours_ne_bloque_pas():
    acquired = sync._sync_lock.acquire(blocking=False)
    assert acquired
    try:
        report = sync.run_sync(dry_run=False, odoo_client_factory=lambda: None)
        assert report.fatal_error == "Une synchronisation est déjà en cours"
    finally:
        sync._sync_lock.release()