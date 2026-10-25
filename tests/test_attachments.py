"""Pièces jointes (images comprises) dans send et reply (24/10, comparaison avec MCP Agent Mail)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import as_agent, call
from mcp.types import CallToolResult

from nexus_relay import box as box_mod
from nexus_relay import server

# PNG 1×1 rouge
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


def _files(tmp_path: Path) -> tuple[Path, Path]:
    image = tmp_path / "capture.png"
    image.write_bytes(PNG)
    note = tmp_path / "notes.txt"
    note.write_text("ligne 12 : jeton visible", encoding="utf-8")
    return image, note


def _blocks(result: Any) -> tuple[dict, list]:
    """Rendu de read : le JSON (bloc texte, identique au structuredContent) puis les blocs image."""
    first, *images = result.content
    assert first.type == "text" and json.loads(first.text) == json.loads(result.structured_content["result"])
    return json.loads(first.text), images


def test_piece_jointe_copiee_dans_la_boite_et_rendue_par_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    image, note = _files(tmp_path)
    sent = call(server.send("codex", "Regarde la capture", topic="t", files=[str(image), str(note)]))
    assert [a["name"] for a in sent["attachments"]] == ["capture.png", "notes.txt"]
    image.unlink()  # la copie de la boîte survit à l'original
    as_agent(monkeypatch, "codex")
    data, images = _blocks(server.read(sent["id"]))
    shown = data["message"]["attachments"]
    assert [a["name"] for a in shown] == ["capture.png", "notes.txt"]
    copy = Path(shown[0]["path"])
    assert copy.read_bytes() == PNG and copy.is_relative_to(server._box().root)
    assert shown[0]["mime"] == "image/png" and shown[1]["size"] == len(b"ligne 12 : jeton visible")
    assert len(images) == 1 and images[0].type == "image" and images[0].mime_type == "image/png"
    salon = (server._box().root / "salon" / "t.md").read_text(encoding="utf-8")
    assert "Pièce jointe : [capture.png](file:///" in salon


def test_piece_jointe_dans_une_reponse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    image, _ = _files(tmp_path)
    asked = call(server.send("antigravity", "Fais une capture", topic="t"))
    as_agent(monkeypatch, "antigravity")
    answer = call(server.reply(asked["id"], "La voici", files=[str(image)]))
    as_agent(monkeypatch, "claude")
    data, images = _blocks(server.read(answer["id"]))
    assert data["message"]["attachments"][0]["name"] == "capture.png" and len(images) == 1


def test_message_sans_piece_jointe_inchange():
    sent = call(server.send("codex", "Rien de joint", topic="t"))
    assert "attachments" not in sent
    data, images = _blocks(server.read(sent["id"]))
    assert "attachments" not in data["message"] and images == []


@pytest.mark.parametrize("bad", ["absent.png", "dossier", "secret"])
def test_pieces_jointes_refusees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str):
    (tmp_path / "dossier").mkdir()
    vault = tmp_path / "coffre"
    vault.mkdir(parents=True)
    (vault / "cle.txt").write_text("sk-secret", encoding="utf-8")
    monkeypatch.setenv("NEXUS_RELAY_VAULTS", str(vault))
    path = {"absent.png": tmp_path / "absent.png", "dossier": tmp_path / "dossier", "secret": vault / "cle.txt"}[bad]
    result = json.loads(server.send("codex", "x", topic="t", files=[str(path)]))
    assert result["status"] == "error"
    assert server._box().all() == []  # rien de déposé à moitié


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "coffre"
    vault.mkdir(parents=True)
    secret = vault / "cle.txt"
    secret.write_text("sk-factice", encoding="utf-8")
    monkeypatch.setenv("NEXUS_RELAY_VAULTS", str(vault))
    return secret


@pytest.mark.skipif(os.name != "nt", reason="chemins Windows")
def test_coffre_par_prefixe_long_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture de Codex du 24/10 : \\\\?\\C:\\…\\coffre\\… passait la comparaison lexicale."""
    secret = _vault(tmp_path, monkeypatch)
    result = json.loads(server.send("codex", "x", topic="t", files=["\\\\?\\" + str(secret)]))
    assert "coffre" in result["error"] and server._box().all() == []


@pytest.mark.skipif(os.name != "nt", reason="partage administratif Windows")
def test_partage_reseau_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relecture d'Antigravity du 24/10 : \\\\localhost\\C$\\…\\coffre\\… échappait à la comparaison par lecteur.
    Le chemin réel du fichier ouvert est simulé (le partage C$ demande les droits d'administrateur)."""
    secret = _vault(tmp_path, monkeypatch)
    unc = "\\\\?\\UNC\\localhost\\c$\\" + str(secret)[3:]
    monkeypatch.setattr(box_mod, "_opened_path", lambda fd: unc)
    assert "réseau" in json.loads(server.send("codex", "x", topic="t", files=[str(secret)]))["error"]


@pytest.mark.skipif(os.name != "nt", reason="jonction Windows")
def test_coffre_par_jonction_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Le contrôle porte sur le fichier ouvert : un dossier public qui est une jonction vers le coffre est refusé
    (le même contrôle couvre la jonction posée entre le contrôle et la copie : il n'y a plus qu'une ouverture)."""
    import _winapi

    secret = _vault(tmp_path, monkeypatch)
    public = tmp_path / "public"
    _winapi.CreateJunction(str(secret.parent), str(public))
    result = json.loads(server.send("codex", "x", topic="t", files=[str(public / "cle.txt")]))
    assert "coffre" in result["error"] and server._box().all() == []


def test_lien_dur_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Un lien dur hors du coffre vers un fichier du coffre porte un autre nom : tout fichier à plusieurs noms."""
    secret = _vault(tmp_path, monkeypatch)
    alias = tmp_path / "alias.txt"
    os.link(secret, alias)
    assert "lien dur" in json.loads(server.send("codex", "x", topic="t", files=[str(alias)]))["error"]


def test_plusieurs_coffres_tous_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """NEXUS_RELAY_VAULTS : plusieurs dossiers séparés par os.pathsep, ~ compris ; chacun refusé."""
    first, second = tmp_path / "un", Path.home() / "deux"
    for vault in (first, second):
        vault.mkdir(parents=True)
        (vault / "cle.txt").write_text("sk-factice", encoding="utf-8")
    monkeypatch.setenv("NEXUS_RELAY_VAULTS", os.pathsep.join([str(first), "~/deux", ""]))
    for vault in (first, second):
        assert "coffre" in json.loads(server.send("codex", "x", topic="t", files=[str(vault / "cle.txt")]))["error"]


def test_sans_coffre_regle_piece_jointe_ordinaire(tmp_path: Path):
    """Sans NEXUS_RELAY_VAULTS : aucun coffre, un fichier ordinaire passe."""
    path = tmp_path / "note.txt"
    path.write_text("bonjour", encoding="utf-8")
    assert json.loads(server.send("codex", "x", topic="t", files=[str(path)]))["status"] == "success"


def test_racine_relative_chemins_absolus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    image, _ = _files(tmp_path)
    monkeypatch.chdir(tmp_path)
    box = box_mod.Box(Path("relais-relatif"))
    message = box.post("claude", "codex", "x", "t", files=[str(image)])
    copy = Path(message.extra["attachments"][0]["path"])
    assert copy.is_absolute() and copy.read_bytes() == PNG
    assert "file:///" in (tmp_path / "relais-relatif" / "salon" / "t.md").read_text(encoding="utf-8")


def test_image_grossie_apres_envoi_pas_rendue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    image, _ = _files(tmp_path)
    sent = call(server.send("codex", "x", topic="t", files=[str(image)]))
    monkeypatch.setattr(server, "INLINE_IMAGE", len(PNG))
    Path(sent["attachments"][0]["path"]).write_bytes(PNG + b"0")
    assert _blocks(server.read(sent["id"]))[1] == []


def test_read_garde_son_schema_et_structured_content(tmp_path: Path):
    """Relecture de Codex : read rendait structuredContent=None et plus de schéma de sortie, même sans image."""
    image, _ = _files(tmp_path)
    plain = call(server.send("codex", "texte seul", topic="t"))["id"]
    joined = call(server.send("codex", "avec image", topic="t", files=[str(image)]))["id"]
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "read")
    assert tool.output_schema and "result" in tool.output_schema["properties"]
    for message_id in (plain, joined):
        result = asyncio.run(server.mcp.call_tool("read", {"id": message_id}))
        assert isinstance(result, CallToolResult) and result.structured_content
        assert json.loads(result.structured_content["result"])["message"]["id"] == message_id


def test_pieces_jointes_bornees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    big = tmp_path / "gros.bin"
    big.write_bytes(b"0" * 64)
    monkeypatch.setattr(box_mod, "MAX_ATTACHMENT", 32)
    assert "trop gros" in json.loads(server.send("codex", "x", topic="t", files=[str(big)]))["error"]
    many = [str(_files(tmp_path)[1])] * (box_mod.MAX_ATTACHMENTS + 1)
    assert "au plus" in json.loads(server.send("codex", "x", topic="t", files=many))["error"]
