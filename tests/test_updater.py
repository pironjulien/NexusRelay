"""Mise à jour du relais par lui-même : état ~/.nexus/status/relay.json, POST/GET /api/update (contrat Nexus)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nexus_relay import __version__, server, updater


def _managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "local" / "NexusRelay" / "app"
    root.mkdir(parents=True)
    (root / "get.ps1").write_text("# get", encoding="ascii")
    return root


def test_derniere_version_lue_dans_la_redirection(monkeypatch: pytest.MonkeyPatch):
    import urllib.error

    class Opener:
        def open(self, request: Any, timeout: float = 0) -> None:
            assert request.full_url == "https://github.com/pironjulien/NexusRelay/releases/latest"
            assert request.get_method() == "HEAD"
            headers = {"Location": "https://github.com/pironjulien/NexusRelay/releases/tag/v9.8.7"}
            raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)  # type: ignore[arg-type]

    monkeypatch.setattr(updater.urllib.request, "build_opener", lambda *handlers: Opener())
    assert updater.fetch_latest() == "9.8.7"


def test_verification_une_fois_par_jour_et_coupable(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return "9.0.0"

    first = updater.check(fetch=fetch)
    assert first["latest"] == "9.0.0" and first["version"] == __version__ and first["product"] == "relay"
    updater.check(fetch=fetch)
    assert calls == [1]  # moins d'un jour : pas de nouvel appel
    monkeypatch.setenv("NEXUS_RELAY_UPDATE_CHECK", "0")
    updater.check(force=True, fetch=fetch)
    assert calls == [1]
    on_disk = json.loads(updater.status_path().read_text(encoding="utf-8"))
    assert set(on_disk) >= {"product", "version", "latest", "state", "checked_at"}


def test_depot_de_developpement_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    code, state = updater.start(fetch=lambda: "99.0.0", root=tmp_path / "depot")
    assert code == 409 and "git" in state["message"] and not updater.marker_path().exists()


def test_deja_a_jour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _managed(tmp_path, monkeypatch)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: pytest.fail("rien lancé"))
    code, state = updater.start(fetch=lambda: __version__, root=root)
    assert code == 200 and state["state"] == "idle" and "up to date" in state["message"]


def test_mise_a_jour_lancee_par_le_demon_puis_issue_notee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _managed(tmp_path, monkeypatch)
    launched: list[dict[str, Any]] = []

    class Proc:
        pid = 4242

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            launched.append({"args": args, **kwargs})

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(updater.subprocess, "Popen", Proc)
    code, state = updater.start(fetch=lambda: "99.0.0", root=root)
    assert code == 202 and state["state"] == "updating"
    (spawn,) = launched
    assert spawn["args"][0] == "powershell.exe" and spawn["args"][-2:] == ["-File", str(root / "get.ps1")]
    assert "-Command" not in spawn["args"] and spawn["env"]["NEXUS_PACK"] == "1"
    assert spawn["creationflags"] & updater.NO_WINDOW == updater.NO_WINDOW
    assert updater.start(fetch=lambda: "99.0.0", root=root)[0] == 202  # déjà en cours : rien de relancé
    assert len(launched) == 1
    # relance du démon sur l'ancien code : get.ps1 n'a pas abouti
    monkeypatch.setattr(updater, "_pid_alive", lambda pid: False)
    assert updater.resume()["state"] == "failed" and not updater.marker_path().exists()
    # relance sur le nouveau code : issue « done »
    updater.marker_path().write_text(json.dumps({"target": __version__, "pid": 1}), encoding="utf-8")
    done = updater.resume()
    assert done["state"] == "done" and not updater.marker_path().exists()
    assert updater.resume()["state"] == "idle"


def _call(app: Any, method: str, token: str | None) -> tuple[int, Any]:
    sent: list[dict[str, Any]] = []
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {"type": "http", "method": method, "path": "/api/update", "headers": headers}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], (json.loads(body) if sent[0]["status"] != 401 else body)


def test_route_du_demon_derriere_le_jeton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "t" * 40
    app = server.http_app(token)
    assert _call(app, "GET", None)[0] == 401
    status, body = _call(app, "GET", token)
    assert status == 200 and body["product"] == "relay" and body["version"] == __version__
    monkeypatch.setattr(updater, "fetch_latest", lambda timeout=10.0: "99.0.0")
    status, body = _call(app, "POST", token)
    assert status == 409  # dépôt de tests : pas une installation publique
    assert _call(app, "DELETE", token)[0] == 405
