"""Le serveur démarre seul, sans Codex ni Antigravity, sur tout système (Windows, Linux) : initialize puis tools/list.

C'est ce que vérifient les annuaires MCP (Glama) dans un conteneur Linux ; la CI le lance aussi sous Linux
(`--noconftest` : ce test se suffit, dans un profil temporaire).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nexus_relay import __version__

TOOLS = {
    "send", "close", "inbox", "read", "reply", "progress", "wait_reply", "track", "reserve", "release",
    "reservations", "search", "summary", "status", "selftest", "where", "survey",
}  # fmt: skip
READ_ONLY = {"inbox", "track", "reservations", "search", "summary", "status", "where"}


def test_initialize_and_list_tools(tmp_path: Path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "NEXUS_RELAY_HOME": str(tmp_path / "relay"),
        "NEXUS_RELAY_GEMINI_HOME": str(tmp_path / "gemini"),
        "NEXUS_JOURNAL_HOME": str(tmp_path / "journal"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "NEXUS_RELAY_AGENT": "claude",
        "NEXUS_RELAY_CODEX_PORT": "1",
    }
    params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "nexus_relay.server"], env=env)

    async def session_check() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "nexus-relay"
            assert init.server_info.version == __version__
            listed = (await session.list_tools()).tools
            # Ce que lit tout client tiers : en anglais, neutre (« the user »), sous la limite de 2048 caractères.
            assert init.instructions and len(init.instructions) <= 2048
            seen = init.instructions + "".join(tool.model_dump_json() for tool in listed)
            for personal in ("Julien", "NexusSecure", "OneDrive", "l'utilisateur", "relais"):
                assert personal not in seen, personal
            tools = {tool.name: tool for tool in listed}
            assert set(tools) == TOOLS
            for name, tool in tools.items():
                hints = tool.annotations
                assert hints is not None and hints.title, name
                assert hints.read_only_hint is (name in READ_ONLY), name
                assert hints.destructive_hint is False and hints.idempotent_hint is not None, name
                assert tool.description and "é" not in tool.description, name  # description anglaise

    async def check() -> None:
        with anyio.fail_after(60):
            await session_check()

    anyio.run(check)
