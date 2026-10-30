"""The real installer JSON parser preserves ISO configuration strings across PowerShell versions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_native_csharp import NO_WINDOW


def test_installer_keeps_cutoff_and_other_iso_strings_verbatim(tmp_path):
    config = {
        "wake_since": "2026-10-29T22:46:43Z",
        "codex_project": "2026-10-30",
        "env": {"timestamp": "2026-10-30T01:00:00+02:00"},
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    fixture = Path(__file__).with_name("native_restart") / "settings.ps1"
    data = json.loads(
        subprocess.check_output(
            ["pwsh", "-NoProfile", "-File", str(fixture), "-InputPath", str(path)], creationflags=NO_WINDOW
        )
    )
    assert data == {
        "cutoff": config["wake_since"],
        "label": config["codex_project"],
        "nested": config["env"]["timestamp"],
    }
