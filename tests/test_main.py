from datetime import datetime, timezone

import app.main as main
from app.sync import SyncReport, PaysSyncResult


def _fake_report(dry_run=False):
    return SyncReport(
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        dry_run=dry_run,
        product_id=100,
        per_pays=[PaysSyncResult(pays="bresil", lots_crees=1)],
    )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_sync_endpoint_returns_report(client, monkeypatch):
    monkeypatch.setattr(main, "run_sync", lambda dry_run=None: _fake_report(dry_run=bool(dry_run)))

    r = client.post("/sync", params={"dry_run": True})
    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True
    assert data["product_id"] == 100
    assert data["per_pays"][0]["pays"] == "bresil"
    assert data["per_pays"][0]["lots_crees"] == 1


def test_last_report_reflects_last_sync_call(client, monkeypatch):
    monkeypatch.setattr(main, "run_sync", lambda dry_run=None: _fake_report(dry_run=False))

    client.post("/sync")
    r = client.get("/sync/last-report")
    assert r.status_code == 200
    assert r.json()["per_pays"][0]["pays"] == "bresil"