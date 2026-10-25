"""Point I du 20/10 : pair fermé, pair sans crédits."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import call

from nexus_relay import cdp, credits, server, wake

# relevé réel du 20/10 (page « Settings › Models » d'Antigravity)
AGY_TEXT = """Models & Usage
Manage your model quota and credits.
Plan
Your Plan: Google AI Pro
Gemini Models
Weekly Limit Remaining
You have used some of your weekly limit, it will fully refresh in 6 days, 9 hours.
33%
Five Hour Limit Remaining
You have used some of your 5-hour limit, it will fully refresh in 14 minutes.
3%
Claude and GPT models
Weekly Limit Remaining
100%
Five Hour Limit Remaining
100%"""


def test_quotas_d_antigravity_lus_et_epuisement():
    usage = cdp.parse_usage(AGY_TEXT)
    assert usage["plan"] == "Google AI Pro" and len(usage["limits"]) == 4
    five = usage["limits"][1]
    assert five == {"family": "Gemini Models", "limit": "5 h", "remaining_pct": 3.0, "resets": "14 minutes"}
    usage.update(status="success", read_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    assert "3 % restants" in (credits.exhausted("antigravity", usage) or "")
    usage["limits"][1]["remaining_pct"] = 40.0
    assert credits.exhausted("antigravity", usage) is None
    assert credits.exhausted("antigravity", None) is None  # inconnu : personne n'est écarté


def test_credits_de_codex_lus_dans_ses_journaux(tmp_path: Path):
    folder = tmp_path / "codex" / "sessions" / "2026" / "10" / "20"
    folder.mkdir(parents=True)
    resets = time.time() + 3600
    limits = {"primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": resets}}
    line = {"timestamp": "2026-10-20T01:00:00Z", "payload": {"type": "token_count", "rate_limits": limits}}
    (folder / "rollout-2026-10-20T01-00-00-x.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    usage = credits.codex_usage(tmp_path / "codex")
    assert usage and usage["windows"]["primary"]["used_pct"] == 97.0
    assert "97 % utilisés (5 h)" in (credits.exhausted("codex", usage) or "")
    old = credits.codex_usage(tmp_path / "codex", now=resets + 1)
    assert credits.exhausted("codex", old) is None  # remise à zéro passée


def test_credits_de_codex_releve_le_plus_recent_meme_dans_un_vieux_chat(tmp_path: Path):
    """Relecture de Codex du 20/10 : le premier relevé trouvé (chat le plus jeune) pouvait dater de 8 h, alors qu'un
    chat plus ancien, repris depuis, portait le dernier."""
    import os

    folder = tmp_path / "codex" / "sessions" / "2026" / "10" / "20"
    folder.mkdir(parents=True)
    resets = time.time() + 3600

    def rollout(name: str, observed: str, used: float, mtime: float) -> None:
        limits = {"primary": {"used_percent": used, "window_minutes": 300, "resets_at": resets}}
        line = {"timestamp": observed, "payload": {"type": "token_count", "rate_limits": limits}}
        path = folder / name
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    for hour in range(10, 20):  # dix chats nés après le vieux, relevés anciens, écrits il y a longtemps
        rollout(f"rollout-2026-10-20T{hour}-00-00-jeune{hour}.jsonl", "2026-10-20T01:00:00Z", 10.0, 1_000_000.0)
    rollout("rollout-2026-10-19T08-00-00-vieux.jsonl", "2026-10-20T21:00:00Z", 88.0, time.time())
    usage = credits.codex_usage(tmp_path / "codex")
    assert usage and usage["observed"] == "2026-10-20T21:00:00Z"
    assert usage["windows"]["primary"]["used_pct"] == 88.0


def _closed(monkeypatch: pytest.MonkeyPatch, running: bool) -> list[int]:
    launched: list[int] = []
    monkeypatch.setattr(wake, "sidecar_alive", lambda box, now=None: False)
    monkeypatch.setattr(cdp, "targets", lambda port=0, timeout=2.0: None)
    monkeypatch.setattr(cdp, "user_in_agy", lambda: False)
    monkeypatch.setattr(cdp, "agy_running", lambda: running)
    monkeypatch.setattr(cdp, "agy_exe", lambda: Path("Antigravity.exe"))
    monkeypatch.setattr(cdp, "_start_detached", lambda path, port: launched.append(port))
    return launched


def test_antigravity_ferme_non_livre_et_relance_avec_son_port(monkeypatch: pytest.MonkeyPatch):
    launched = _closed(monkeypatch, running=False)
    result = wake._antigravity(["20261020T000000000-abcdef"])
    assert result["status"] == "failed" and result["reason"].startswith("non livré : Antigravity fermé")
    assert launched == [9227]  # son port, même si les tests ferment le port d'Antigravity


def test_antigravity_ouvert_sans_voie_relance_s_il_ne_travaille_pas(monkeypatch: pytest.MonkeyPatch):
    """23/10 (l'utilisateur) : ouvert sans port ni sidecar, il est relancé avec son port ; jamais pendant un travail."""
    launched = _closed(monkeypatch, running=True)
    stopped: list[str] = []
    monkeypatch.setattr(cdp, "_ps", lambda command, timeout=30.0: stopped.append(command) or "")
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "agy_working", lambda now=None: True)
    result = wake._antigravity(["20261020T000000000-abcdef"])
    assert result["status"] == "deferred" and "travaille" in result["reason"] and stopped == [] and launched == []
    monkeypatch.setattr(cdp, "agy_working", lambda now=None: False)
    result = wake._antigravity(["20261020T000000000-abcdef"])
    assert "ouvert sans sidecar" in result["reason"] and len(stopped) == 1 and launched == [9227]


def _codex_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credits, "availability", lambda box, agent, **_: "Codex : 99 % utilisés (5 h)" if agent == "codex" else None
    )


def test_wait_reply_dit_l_indisponibilite_tout_de_suite(monkeypatch: pytest.MonkeyPatch):
    _codex_out(monkeypatch)
    monkeypatch.setattr(server, "WAIT_POLL_S", 0.02)
    monkeypatch.setattr(server, "UNAVAILABLE_CHECK_S", 0.0)
    sent = call(server.send("codex", "q", topic="t"))
    start = time.time()
    result = json.loads(asyncio.run(server.wait_reply(sent["id"], timeout_s=60)))
    assert result["status"] == "unavailable" and "99 %" in result["reason"] and time.time() - start < 5


def test_antigravity_launched_with_neutral_stdio(monkeypatch) -> None:
    """22/10 : sorties héritées du démon, fermées à son redémarrage → « write EPIPE » dans Antigravity."""
    seen: dict = {}
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda *args, **kwargs: seen.update(kwargs))
    cdp._start_detached(Path("antigravity.exe"), 9227)
    assert seen["stdin"] is seen["stdout"] is seen["stderr"] is cdp.subprocess.DEVNULL
