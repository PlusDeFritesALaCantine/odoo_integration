import socket

import pytest
import xmlrpc.client

from app.odoo_client import OdooAuthError, OdooClient, OdooConnectionError, OdooRpcError
from tests.fakes import FakeOdooCommon, FakeOdooObject, RaisingCommon


def make_client(common=None, obj=None, **kwargs):
    return OdooClient(
        url="https://odoo.example.test",
        db="futurekawa",
        username="integration@futurekawa.local",
        api_key="fake-api-key",
        common_proxy=common or FakeOdooCommon(),
        object_proxy=obj or FakeOdooObject(),
        **kwargs,
    )


def test_authenticate_sets_uid():
    client = make_client(common=FakeOdooCommon(uid=42))
    uid = client.authenticate()
    assert uid == 42
    assert client.uid == 42


def test_authenticate_invalid_credentials_raises_auth_error():
    common = FakeOdooCommon(valid_db="futurekawa", valid_login="ok", valid_api_key="ok")
    client = make_client(common=common)
    with pytest.raises(OdooAuthError):
        client.authenticate()


def test_authenticate_connection_error():
    client = make_client(common=RaisingCommon(socket.timeout("timed out")))
    with pytest.raises(OdooConnectionError):
        client.authenticate()


def test_create_then_search_read():
    obj = FakeOdooObject()
    client = make_client(obj=obj)
    new_id = client.create("stock.lot", {"name": "bresil:L1", "x_futurekawa_lot_id": "bresil:L1"})
    assert new_id == 1

    results = client.search_read(
        "stock.lot", [["x_futurekawa_lot_id", "=", "bresil:L1"]], ["id", "name"]
    )
    assert results == [{"id": 1, "name": "bresil:L1"}]

    model, method, args, kwargs = obj.calls[0]
    assert (model, method) == ("stock.lot", "create")
    assert args == [{"name": "bresil:L1", "x_futurekawa_lot_id": "bresil:L1"}]


def test_write_updates_existing_record():
    obj = FakeOdooObject()
    obj.seed("stock.lot", 1, {"x_futurekawa_statut": "conforme"})
    client = make_client(obj=obj)

    ok = client.write("stock.lot", [1], {"x_futurekawa_statut": "perime"})
    assert ok is True
    assert obj._store["stock.lot"][1]["x_futurekawa_statut"] == "perime"


def test_message_post_and_activity_schedule_recorded():
    obj = FakeOdooObject()
    client = make_client(obj=obj)

    client.message_post("stock.lot", 1, "Alerte FutureKawa : lot périmé")
    client.activity_schedule("stock.lot", 1, summary="Lot périmé", note="détails", user_id=9)

    assert obj.messages == [("stock.lot", 1, "Alerte FutureKawa : lot périmé")]
    assert obj.activities == [("stock.lot", 1, "Lot périmé", "détails", 9)]


def test_post_to_channel_success():
    obj = FakeOdooObject()
    obj.seed("discuss.channel", 12, {"name": "Alertes"})
    client = make_client(obj=obj)

    msg_id = client.post_to_channel("Alertes", "Alerte sur le lot L1")

    assert msg_id == 1
    assert obj.messages == [("discuss.channel", 12, "Alerte sur le lot L1")]


def test_post_to_channel_fallback_mail_channel():
    obj = FakeOdooObject()
    obj.fail_next(
        "discuss.channel",
        "search_read",
        xmlrpc.client.Fault(1, "Object discuss.channel doesn't exist"),
    )
    obj.seed("mail.channel", 15, {"name": "Alertes"})
    client = make_client(obj=obj)

    msg_id = client.post_to_channel("Alertes", "Alerte fallback mail.channel")

    assert msg_id == 1
    assert obj.messages == [("mail.channel", 15, "Alerte fallback mail.channel")]


def test_post_to_channel_not_found_raises_rpc_error():
    obj = FakeOdooObject()
    client = make_client(obj=obj)

    with pytest.raises(OdooRpcError, match="Canal de discussion introuvable : #Inexistant"):
        client.post_to_channel("Inexistant", "Message d'alerte")


def test_fault_raises_odoo_rpc_error():
    obj = FakeOdooObject()
    obj.fail_next(
        "stock.lot", "create", xmlrpc.client.Fault(1, "Contrainte unique violée : x_futurekawa_lot_id")
    )
    client = make_client(obj=obj)

    with pytest.raises(OdooRpcError):
        client.create("stock.lot", {"name": "bresil:L1"})


def test_resolve_user_id_found_and_not_found():
    obj = FakeOdooObject()
    obj.seed("res.users", 9, {"login": "qualite@futurekawa.local"})
    client = make_client(obj=obj)

    assert client.resolve_user_id("qualite@futurekawa.local") == 9
    assert client.resolve_user_id("inconnu@futurekawa.local") is None


def test_execute_authenticates_lazily_if_needed():
    common = FakeOdooCommon(uid=5)
    obj = FakeOdooObject()
    client = make_client(common=common, obj=obj)
    assert client.uid is None

    client.create("stock.lot", {"name": "bresil:L1"})
    assert client.uid == 5
    assert len(common.authenticate_calls) == 1