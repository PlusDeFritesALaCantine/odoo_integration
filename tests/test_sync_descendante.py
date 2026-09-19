"""Synchronisation descendante : Odoo -> FutureKawa.

La synchro était unidirectionnelle : une correction saisie dans Odoo était
écrasée au cycle suivant par la valeur FutureKawa. Ces tests verrouillent
l'arbitrage par horodatage et le fait qu'aucun PATCH inutile ne part.
"""

import httpx
import pytest
import respx

from app import config, sync
from app.mapping import champs_a_remonter, decoder_business_key, modifie_dans_odoo
from tests.fakes import FakeOdooCommon, FakeOdooObject
from tests.test_sync import BACKEND_URL, PRODUCT_CODE, make_factory, mock_pays

LOT = {
    "id": "L1", "pays": "bresil", "exploitation": "Fazenda X",
    "entrepot_id": "e1", "date_stockage": "2026-05-19", "statut": "conforme",
}


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr(config, "BACKEND_FUTUREKAWA_URL", BACKEND_URL)
    monkeypatch.setattr(config, "ODOO_DEFAULT_PRODUCT_DEFAULT_CODE", PRODUCT_CODE)
    monkeypatch.setattr(config, "ODOO_ACTIVITY_USER_LOGIN", "")
    sync._derniere_alerte_notifiee.clear()
    yield
    sync._derniere_alerte_notifiee.clear()


def _seed_lot_odoo(obj, *, exploitation="Fazenda X", entrepot="e1",
                   date_stockage="2026-05-19", write_date, derniere_sync):
    obj.seed("stock.lot", 500, {
        "name": "bresil:L1",
        "x_futurekawa_lot_id": "bresil:L1",
        "x_futurekawa_pays": "bresil",
        "x_futurekawa_exploitation": exploitation,
        "x_futurekawa_entrepot_id": entrepot,
        "x_futurekawa_date_stockage": date_stockage,
        "x_futurekawa_derniere_sync_le": derniere_sync,
        "write_date": write_date,
    })


class TestArbitrageHorodatage:
    """modifie_dans_odoo() décide qui fait foi. C'est le cœur de la règle."""

    def test_notre_propre_ecriture_ne_compte_pas(self):
        record = {
            "write_date": "2026-09-19 10:00:02",
            "x_futurekawa_derniere_sync_le": "2026-09-19 10:00:00",
        }
        # 2 secondes d'écart : c'est notre write qui a bougé write_date.
        assert modifie_dans_odoo(record) is False

    def test_edition_humaine_posterieure(self):
        record = {
            "write_date": "2026-09-19 11:30:00",
            "x_futurekawa_derniere_sync_le": "2026-09-19 10:00:00",
        }
        assert modifie_dans_odoo(record) is True

    def test_jamais_synchronise_compte_comme_modifie(self):
        record = {"write_date": "2026-09-19 11:30:00", "x_futurekawa_derniere_sync_le": False}
        assert modifie_dans_odoo(record) is True

    def test_sans_write_date_on_ne_remonte_rien(self):
        assert modifie_dans_odoo({"write_date": False}) is False


class TestChampsRemontes:
    def test_seuls_les_champs_differents_remontent(self):
        record = {
            "x_futurekawa_exploitation": "Fazenda Corrigee",
            "x_futurekawa_entrepot_id": "e1",
            "x_futurekawa_date_stockage": "2026-05-19",
        }
        assert champs_a_remonter(record, LOT) == {"exploitation": "Fazenda Corrigee"}

    def test_aucun_ecart_ne_produit_aucun_champ(self):
        record = {
            "x_futurekawa_exploitation": "Fazenda X",
            "x_futurekawa_entrepot_id": "e1",
            "x_futurekawa_date_stockage": "2026-05-19",
        }
        assert champs_a_remonter(record, LOT) == {}

    def test_un_champ_vide_dans_odoo_n_efface_pas_futurekawa(self):
        record = {"x_futurekawa_exploitation": False, "x_futurekawa_entrepot_id": "e2"}
        assert champs_a_remonter(record, LOT) == {"entrepot_id": "e2"}

    def test_le_statut_et_les_releves_ne_sont_jamais_remontes(self):
        record = {
            "x_futurekawa_statut": "conforme",
            "x_futurekawa_derniere_temperature": 12.0,
            "x_futurekawa_exploitation": "Fazenda X",
        }
        assert champs_a_remonter(record, LOT) == {}

    def test_decodage_de_la_cle_metier(self):
        assert decoder_business_key("bresil:LOT-BR-001") == ("bresil", "LOT-BR-001")
        assert decoder_business_key("sans-separateur") is None
        assert decoder_business_key("") is None


class TestPasseDescendante:
    @respx.mock
    def test_une_correction_odoo_remonte_dans_futurekawa(self):
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, exploitation="Fazenda Corrigee",
                       write_date="2026-09-19 11:30:00",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        patch_route = respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1").mock(
            return_value=httpx.Response(200, json={**LOT, "exploitation": "Fazenda Corrigee"})
        )

        rapport = sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        assert rapport.per_pays[0].lots_remontes == 1
        assert patch_route.called
        import json
        assert json.loads(patch_route.calls[0].request.content) == {
            "exploitation": "Fazenda Corrigee"
        }

    @respx.mock
    def test_aucun_patch_si_rien_n_a_change(self):
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, write_date="2026-09-19 11:30:00",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        patch_route = respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1")

        rapport = sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        assert rapport.per_pays[0].lots_remontes == 0
        assert not patch_route.called

    @respx.mock
    def test_aucun_patch_si_seule_notre_ecriture_a_bouge(self):
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, exploitation="Fazenda Corrigee",
                       write_date="2026-09-19 10:00:02",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        patch_route = respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1")

        rapport = sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        assert rapport.per_pays[0].lots_remontes == 0
        assert not patch_route.called

    @respx.mock
    def test_dry_run_compte_sans_ecrire(self):
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, entrepot="e-corrige",
                       write_date="2026-09-19 11:30:00",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        patch_route = respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1")

        rapport = sync.run_sync(dry_run=True, odoo_client_factory=make_factory(obj))

        assert rapport.per_pays[0].lots_remontes == 1
        assert not patch_route.called

    @respx.mock
    def test_lot_present_dans_odoo_mais_inconnu_de_futurekawa_est_ignore(self):
        obj = FakeOdooObject()
        obj.seed("stock.lot", 501, {
            "name": "bresil:FANTOME",
            "x_futurekawa_lot_id": "bresil:FANTOME",
            "x_futurekawa_pays": "bresil",
            "x_futurekawa_exploitation": "Inventee",
            "write_date": "2026-09-19 11:30:00",
            "x_futurekawa_derniere_sync_le": "2026-09-19 10:00:00",
        })
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})

        rapport = sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        # La création de lot reste du ressort de FutureKawa : on n'invente rien.
        assert rapport.per_pays[0].lots_remontes == 0
        assert rapport.per_pays[0].erreurs == []

    @respx.mock
    def test_echec_du_patch_est_rapporte_sans_interrompre_le_cycle(self):
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, exploitation="Fazenda Corrigee",
                       write_date="2026-09-19 11:30:00",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1").mock(
            return_value=httpx.Response(404, json={"detail": "Lot introuvable"})
        )

        rapport = sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        assert rapport.per_pays[0].erreurs
        assert "Remontée Odoo" in rapport.per_pays[0].erreurs[0]
        assert rapport.fatal_error is None   # le cycle va jusqu'au bout

    @respx.mock
    def test_la_passe_montante_repart_des_valeurs_remontees(self):
        """Sans mise à jour locale du lot, la passe montante réécrirait aussitôt
        l'ancienne valeur dans Odoo — la correction humaine ferait un aller-retour
        pour revenir à son point de départ."""
        obj = FakeOdooObject()
        _seed_lot_odoo(obj, exploitation="Fazenda Corrigee",
                       write_date="2026-09-19 11:30:00",
                       derniere_sync="2026-09-19 10:00:00")
        mock_pays(["bresil"], {"bresil": [dict(LOT)]})
        respx.patch(f"{BACKEND_URL}/pays/bresil/lots/L1").mock(
            return_value=httpx.Response(200, json={**LOT, "exploitation": "Fazenda Corrigee"})
        )

        sync.run_sync(dry_run=False, odoo_client_factory=make_factory(obj))

        ecritures = [c for c in obj.calls if c[1] == "write" and c[0] == "stock.lot"]
        assert ecritures, "la passe montante doit avoir écrit dans Odoo"
        valeurs = ecritures[-1][2][1]
        assert valeurs["x_futurekawa_exploitation"] == "Fazenda Corrigee"
