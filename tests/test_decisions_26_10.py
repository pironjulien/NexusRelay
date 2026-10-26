"""Points tranchés par l'utilisateur le 26/10 : alias de réservation de mcp_agent_mail, relance encadrée
d'Antigravity ouvert sans son port."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import as_agent, call

from nexus_relay import cdp, reservations, server

# --- alias de mcp_agent_mail


def test_alias_agent_mail_reserve_puis_libere_par_chemin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    file = str(tmp_path / "src" / "a.py")
    held = call(server.file_reservation_paths([file], "correctif", ttl_seconds=90, project_key="x", agent_name="y"))
    assert held["paths"] == [file]
    assert reservations.active(server._box())[0]["agent"] == "claude"  # agent_name ignoré : le relais sait qui appelle
    as_agent(monkeypatch, "codex")
    refused = json.loads(server.file_reservation_paths([str(tmp_path / "src")]))
    assert refused["status"] == "error" and "claude" in refused["error"]
    as_agent(monkeypatch, "claude")
    released = call(server.release_file_reservations(paths=[str(tmp_path / "src")]))
    assert released["released"] and not reservations.active(server._box())


def test_alias_agent_mail_libere_par_id_ou_tout(tmp_path: Path):
    a = call(server.file_reservation_paths([str(tmp_path / "a.py")], "un"))
    call(server.file_reservation_paths([str(tmp_path / "b.py")], "deux"))
    assert call(server.release_file_reservations(file_reservation_ids=[a["id"]]))["released"] == [a["id"]]
    assert len(call(server.release_file_reservations())["released"]) == 1


# --- Antigravity ouvert sans son port


def test_antigravity_sans_port_relance_encadree(monkeypatch: pytest.MonkeyPatch):
    restarts: list[str] = []
    monkeypatch.setattr(cdp, "agy_page_url", lambda port=0: None)
    monkeypatch.setattr(cdp, "agy_running", lambda: True)
    monkeypatch.setattr(cdp, "_restart_antigravity", lambda why, port: restarts.append(why) or {"launched": True})
    monkeypatch.setitem(cdp._agy_port, "checked", 0.0)
    monkeypatch.setitem(cdp._agy_port, "restarted", 0.0)
    t = 1_000_000.0
    assert cdp.restart_portless_antigravity(now=t) is None  # premier relevé : le compte commence
    assert cdp.restart_portless_antigravity(now=t + 30) is None  # relevé au plus une fois par minute
    assert cdp.restart_portless_antigravity(now=t + 200) is None  # pas encore 5 min
    assert cdp.restart_portless_antigravity(now=t + 400) == {"launched": True}
    assert len(restarts) == 1
    for step in range(1, 30):  # toujours sans port ensuite : jamais plus d'une relance par heure
        result = cdp.restart_portless_antigravity(now=t + 400 + step * 100)
        assert result is None or result.get("status") == "deferred"
    assert len(restarts) == 1


def test_antigravity_avec_son_port_jamais_relance(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cdp, "agy_page_url", lambda port=0: "ws://agy")
    monkeypatch.setattr(cdp, "_restart_antigravity", lambda why, port: pytest.fail("relancé avec son port"))
    monkeypatch.setitem(cdp._agy_port, "checked", 0.0)
    for step in range(20):
        assert cdp.restart_portless_antigravity(now=2_000_000.0 + step * 100) is None
