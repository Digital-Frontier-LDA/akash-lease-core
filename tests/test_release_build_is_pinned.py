"""The release build backend is pinned, so a published sha256 is reproducible (#50 L5)."""

from __future__ import annotations

import re
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"

# The measured closure of build==1.6.1 + hatchling==1.32.0 on the release job's
# Python 3.11 (tomli and importlib-metadata are marker-excluded there). With
# `--no-deps` this set IS the build environment, so each member must be pinned.
CLOSED_BUILD_SET = frozenset(
    {
        "build",
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "pyproject_hooks",
        "tomlkit",
        "trove-classifiers",
    }
)


def _build_step_script(text: str) -> str:
    marker = "      - name: Build sdist and wheel\n"
    assert text.count(marker) == 1, "the release build step is no longer uniquely identifiable"
    body = text.split(marker, 1)[1]
    return body.split("\n      - ", 1)[0]


def _install_line(script: str) -> str:
    joined = script.replace("\\\n", " ")
    installs = [line for line in joined.splitlines() if "pip install" in line]
    assert len(installs) == 1, f"expected one install in the build step, found {len(installs)}"
    return installs[0].split("pip install", 1)[1]


def _install_requirements(script: str) -> list[str]:
    return [token for token in _install_line(script).split() if token[0] != "-"]


def _errors(text: str) -> list[str]:
    script = _build_step_script(text)
    requirements = _install_requirements(script)
    errors = [
        f"{token} is not pinned to an exact version" for token in requirements if "==" not in token
    ]
    names = {re.split(r"==", token, maxsplit=1)[0].lower() for token in requirements}
    errors += [
        f"{name} is not installed by the build step"
        for name in sorted(CLOSED_BUILD_SET)
        if name not in names
    ]
    if "--no-deps" not in _install_line(script).split():
        errors.append("the install must use --no-deps so the pinned set is closed")
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


def test_dropping_a_transitive_pin_turns_the_guard_red() -> None:
    text = RELEASE.read_text()
    target = "            packaging==26.3 \\\n"
    assert text.count(target) == 1
    assert _errors(text.replace(target, "")) == ["packaging is not installed by the build step"]


def test_resolving_dependencies_from_the_index_turns_the_guard_red() -> None:
    text = RELEASE.read_text()
    target = "pip install --quiet --no-deps"
    assert text.count(target) == 1
    assert _errors(text.replace(target, "pip install --quiet")) == [
        "the install must use --no-deps so the pinned set is closed"
    ]
