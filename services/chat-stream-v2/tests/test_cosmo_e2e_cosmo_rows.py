"""Cosmo E2E cosmo rows (unit tier): calendar add/delete + list check/undo with
the server-owned undo window, driven against the REAL disposable Cosmo server
(create_app + TestClient + advanceable clock).

Skips with an explicit reason if cosmo_server is unavailable (e.g. the public CI
gate, which has no cosmo checkout) — never a silent pass.
"""
from __future__ import annotations

import pytest

from tests.cosmo_e2e import harness as H


def _cosmo(tmp_path):
    try:
        return H.build_cosmo(tmp_path)
    except ImportError as exc:  # cosmo_server not on path (e.g. public CI)
        pytest.skip(f"cosmo_server unavailable (set COSMO_SERVER_DIR): {exc}")


def test_calendar_add_then_delete(tmp_path):
    client, _clock, tokens = _cosmo(tmp_path)
    phone = {"Authorization": f"Bearer {tokens['phone']}"}
    with client:
        # empty events collection
        day = "2026-05-10"
        got = client.get(f"/events?from={day}&to={day}", headers=phone)
        assert got.status_code == 200 and got.json()["items"] == []
        # POST /events (201) -> server-owned id
        created = client.post("/events", headers=phone,
                              json={"date": day, "title": "Cosmo E2E event"})
        assert created.status_code == 201, created.text
        eid = created.json()["id"]
        present = client.get(f"/events?from={day}&to={day}", headers=phone).json()["items"]
        assert any(e["id"] == eid for e in present), present
        # DELETE /events/{id} (204) -> absent
        assert client.delete(f"/events/{eid}", headers=phone).status_code == 204
        after = client.get(f"/events?from={day}&to={day}", headers=phone).json()["items"]
        assert all(e["id"] != eid for e in after), after


def test_list_undo_window_two_items(tmp_path):
    """I_undo is restored within the window; I_expire, left done past the window,
    refuses undo with HTTP 410."""
    client, clock, tokens = _cosmo(tmp_path)
    from cosmo_server import completion  # importable once build_cosmo succeeded
    phone = {"Authorization": f"Bearer {tokens['phone']}"}
    with client:
        def add(label):
            return client.post("/lists/grocery/items", headers=phone, json={"label": label}).json()["id"]

        i_undo = add("undo-me")
        i_expire = add("expire-me")
        # both done at T0
        assert client.patch(f"/lists/items/{i_undo}/done", headers=phone).status_code == 200
        assert client.patch(f"/lists/items/{i_expire}/done", headers=phone).status_code == 200
        # (a) undo I_undo within the window -> restored (done_at NULL), still listed
        undone = client.patch(f"/lists/items/{i_undo}/undone", headers=phone)
        assert undone.status_code == 200, undone.text
        assert undone.json()["done_at"] is None
        listed = [i["id"] for i in client.get("/lists/grocery/items", headers=phone).json()["items"]]
        assert i_undo in listed
        # (b) advance the injected clock past the undo WINDOW, leaving I_expire done
        clock.advance(completion.WINDOW + 1.0)
        # (c) undo I_expire after the window -> HTTP 410 'item expired'
        expired = client.patch(f"/lists/items/{i_expire}/undone", headers=phone)
        assert expired.status_code == 410, expired.text
        # I_undo (never expired) can still be re-done/undone
        assert client.patch(f"/lists/items/{i_undo}/done", headers=phone).status_code == 200
