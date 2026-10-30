"""Installer repairs absent plugin activation and preserves explicit disabled choices and unrelated settings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_native_csharp import NO_WINDOW


def test_missing_plugin_is_activated_alongside_existing_sidecar_without_losing_configuration(tmp_path):
    config = {
        "plugins": {"other": {"enabled": True, "nested": "keep"}},
        "sidecars": {"nexus-relay/relay": {"enabled": True, "projectId": "fixture-project", "schemaVersion": 1}},
        "userSettings": {"keep": "value"},
    }
    script = Path(__file__).with_name("native_restart") / "activation.ps1"

    def run(data):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return json.loads(
            subprocess.check_output(
                ["pwsh", "-NoProfile", "-File", str(script), "-InputPath", str(path)], creationflags=NO_WINDOW
            )
        )

    result = run(config)
    assert result["Changed"] and result["Config"]["plugins"]["nexus-relay"]["enabled"] is True
    assert (
        result["Config"]["userSettings"] == config["userSettings"]
        and result["Config"]["plugins"]["other"] == config["plugins"]["other"]
    )
    assert result["Config"]["sidecars"]["nexus-relay/relay"]["schemaVersion"] == 1
    assert run(result["Config"])["Changed"] is False
    for section, name in (("plugins", "nexus-relay"), ("sidecars", "nexus-relay/relay")):
        disabled = json.loads(json.dumps(result["Config"]))
        disabled[section][name]["enabled"] = False
        preserved = run(disabled)
        assert preserved["Disabled"] and preserved["Changed"] is False and preserved["Config"] == disabled
