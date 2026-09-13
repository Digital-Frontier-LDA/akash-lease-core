"""The release build backend is pinned, so a published sha256 is reproducible (#50 L5)."""

from __future__ import annotations

import re
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _build_step_script(text: str) -> str:
    marker = "      - name: Build sdist and wheel\n"
    assert text.count(marker) == 1, "the release build step is no longer uniquely identifiable"
    body = text.split(marker, 1)[1]
    return body.split("\n      - ", 1)[0]


def _install_requirements(script: str) -> list[str]:
    joined = script.replace("\\\n", " ")
    installs = [line for line in joined.splitlines() if "pip install" in line]
    assert len(installs) == 1, f"expected one install in the build step, found {len(installs)}"
    return [token for token in installs[0].split("pip install", 1)[1].split() if token[0] != "-"]


def _errors(text: str) -> list[str]:
    script = _build_step_script(text)
    requirements = _install_requirements(script)
    errors = [
        f"{token} is not pinned to an exact version" for token in requirements if "==" not in token
    ]
    names = {re.split(r"==", token, maxsplit=1)[0].lower() for token in requirements}
    errors += [
        f"{name} is not installed by the build step"
        for name in ("build", "hatchling")
        if name not in names
    ]
    builds = [line.strip() for line in script.splitlines() if "-m build" in line]
    if builds != ["python3 -m build --no-isolation"]:
        errors.append(f"the build must run without isolation so the pins are what build: {builds}")
    return errors


def test_the_release_backend_is_pinned_and_actually_used() -> None:
    assert _errors(RELEASE.read_text()) == []


def test_unpinning_hatchling_turns_the_guard_red() -> None:
    text = RELEASE.read_text()
    target = "hatchling==1.32.0"
    assert text.count(target) == 1
    assert _errors(text.replace(target, "hatchling")) == [
        "hatchling is not pinned to an exact version"
    ]


def test_restoring_an_isolated_build_turns_the_guard_red() -> None:
    text = RELEASE.read_text()
    target = "python3 -m build --no-isolation"
    assert text.count(target) == 1
    errors = _errors(text.replace(target, "python3 -m build"))
    assert len(errors) == 1 and errors[0].startswith("the build must run without isolation")
