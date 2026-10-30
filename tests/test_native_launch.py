"""Real Explorer broker against an invisible fixture under caller KILL_ON_JOB_CLOSE."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from test_native_csharp import NO_WINDOW, ROOT


def test_user_application_survives_caller_job_with_negative_control():
    output = Path(os.environ["LOCALAPPDATA"]) / "NexusRelay/build/tests/lifetime"
    build = subprocess.run(
        [
            "dotnet",
            "publish",
            str(ROOT / "tests/native_lifetime/LifetimeFixture.csproj"),
            "-c",
            "Release",
            "-o",
            str(output),
            "--nologo",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=NO_WINDOW,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(output / "LifetimeFixture.exe"), "--test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        creationflags=NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {
            "ok": True,
            "broker": broker,
            "caller_in_job": True,
            "child_in_caller_job": not broker,
            "survived_job_close": broker,
            "child_normal_exit": broker,
        }
        for broker in (False, True)
    ]
    assert not (ROOT / "tests/native_lifetime/bin").exists() and not (ROOT / "tests/native_lifetime/obj").exists()
