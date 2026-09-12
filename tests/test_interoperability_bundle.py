from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ("canonicalization.json", "coverage.json", "model.json", "vectors.json")


def _copied_bundle(tmp_path):
    relative_bundle = Path("interoperability") / "akash-lifecycle-interoperability-v1"
    source = ROOT / relative_bundle
    target = tmp_path / relative_bundle
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)
    return target


def _node_result(node, tmp_path):
    environment = os.environ.copy()
    environment["AKASH_INTEROP_REPOSITORY_ROOT"] = str(tmp_path)
    return subprocess.run(
        [node, ROOT / "tools/verify_interoperability_bundle.mjs"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _required_node():
    node = shutil.which("node")
    assert node is not None, "Node is required to verify the independent interoperability adapter"
    return node


def _canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _write_artifact_and_rebind(target, artifact, payload):
    artifact_bytes = _canonical_bytes(payload)
    (target / artifact).write_bytes(artifact_bytes)
    manifest_path = target / "bundle.json"
    manifest = json.loads(manifest_path.read_text())
    entries = [item for item in manifest["artifacts"] if item["path"] == artifact]
    assert len(entries) == 1
    entries[0]["digest"] = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    manifest_path.write_bytes(_canonical_bytes(manifest))


def test_python_model_regenerates_exact_published_bundle():
    result = subprocess.run(
        [sys.executable, "tools/build_interoperability_bundle.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "verified 5 bundle artifacts" in result.stdout


def test_independent_node_adapter_agrees_on_bytes_digests_and_outcomes():
    node = _required_node()
    result = subprocess.run(
        [node, "tools/verify_interoperability_bundle.mjs"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "verified 18 vectors" in result.stdout


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_each_artifact_byte_mutation_is_observable(tmp_path, artifact):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    artifact_path = target / artifact
    payload = artifact_path.read_text()
    mutated = payload.replace("{", '{"mutation":true,', 1)
    assert mutated != payload
    artifact_path.write_text(mutated)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "digest mismatch:" in result.stderr
    assert artifact in result.stderr


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_each_artifact_path_mutation_is_observable(tmp_path, artifact):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    manifest_path = target / "bundle.json"
    manifest = manifest_path.read_text()
    locator = f'"path":"{artifact}"'
    mutated = manifest.replace(locator, f'"path":"{artifact}.wrong"', 1)
    assert mutated != manifest
    manifest_path.write_text(mutated)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "locator mismatch:" in result.stderr


def test_bundle_identity_mutation_is_observable(tmp_path):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    manifest_path = target / "bundle.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["core_model"]["version"] == "0.12.0"
    manifest["core_model"]["version"] = "999.0.0"
    manifest_path.write_bytes(_canonical_bytes(manifest))

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "bundle identity mismatch" in result.stderr


@pytest.mark.parametrize("mutation", ("status", "reason", "population"))
def test_closed_role_role_semantics_survive_a_matching_digest(tmp_path, mutation):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    coverage_path = target / "coverage.json"
    coverage = json.loads(coverage_path.read_text())
    role = coverage["roles"]["transaction.atomic_membership"]
    assert role["status"] == "unavailable_fail_closed"
    if mutation == "status":
        role["status"] = "supported"
    elif mutation == "reason":
        role["reason"] = ""
    else:
        removed = coverage["normative_role_population"].pop()
        assert removed
    _write_artifact_and_rebind(target, "coverage.json", coverage)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "coverage role" in result.stderr


@pytest.mark.parametrize("mutation", ("required", "type", "enum", "unused_type", "root"))
def test_machine_model_semantics_survive_a_matching_digest(tmp_path, mutation):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    model = json.loads((target / "model.json").read_text())
    revision = next(
        field
        for field in model["records"]["AdmissionState"]["fields"]
        if field["name"] == "revision"
    )
    if mutation == "required":
        assert revision["required"] is True
        revision["required"] = False
    elif mutation == "type":
        assert revision["type"] == "int"
        revision["type"] = "str"
    elif mutation == "enum":
        model["enums"]["AdmissionDisposition"][0] = "invented"
    elif mutation == "unused_type":
        field = next(
            field
            for field in model["records"]["CreatePermit"]["fields"]
            if field["name"] == "state_revision"
        )
        assert field["type"] == "int"
        field["type"] = "str"
    else:
        model["wire_roots"].append("InventedRoot")
    _write_artifact_and_rebind(target, "model.json", model)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "machine" in result.stderr or "expected str" in result.stderr


def test_measured_outcome_population_survives_a_matching_digest(tmp_path):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    coverage = json.loads((target / "coverage.json").read_text())
    coverage["measured_outcomes"].append("invented_success")
    _write_artifact_and_rebind(target, "coverage.json", coverage)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "measured-outcome population mismatch" in result.stderr


def test_lone_surrogate_key_is_rejected_with_matching_digests(tmp_path):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    fixture = json.loads((target / "vectors.json").read_text())
    vector = next(
        item
        for item in fixture["vectors"]
        if item["id"] == "canonical-json-control-astral-and-key-order"
    )
    vector["input"]["entries"].append(("\ud800", "lone-high-surrogate"))
    value = dict(vector["input"]["entries"])
    vector["expected"]["canonical_json"] = _canonical_bytes(value).decode("ascii")
    payload = {"input": vector["input"], "expected": vector["expected"]}
    payload_bytes = _canonical_bytes(payload)
    vector["canonical_payload_sha256"] = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
    vector["canonical_payload_utf8_hex"] = payload_bytes.hex()
    _write_artifact_and_rebind(target, "vectors.json", fixture)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "Unicode scalar values" in result.stderr


def test_canonicalization_rule_semantics_survive_a_matching_digest(tmp_path):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    rules = json.loads((target / "canonicalization.json").read_text())
    assert rules["json_profile"]["object_key_order"] == "ascending Unicode code point"
    rules["json_profile"]["object_key_order"] = "insertion order"
    _write_artifact_and_rebind(target, "canonicalization.json", rules)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "canonicalization rule mismatch" in result.stderr


def test_control_astral_and_key_order_corpus_survives_self_consistent_digests(tmp_path):
    node = _required_node()
    target = _copied_bundle(tmp_path)
    fixture = json.loads((target / "vectors.json").read_text())
    matches = [
        item
        for item in fixture["vectors"]
        if item["id"] == "canonical-json-control-astral-and-key-order"
    ]
    assert len(matches) == 1
    vector = matches[0]
    vector["expected"]["canonical_json"] = "{}"
    payload = {"input": vector["input"], "expected": vector["expected"]}
    payload_bytes = _canonical_bytes(payload)
    vector["canonical_payload_sha256"] = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
    vector["canonical_payload_utf8_hex"] = payload_bytes.hex()
    _write_artifact_and_rebind(target, "vectors.json", fixture)

    result = _node_result(node, tmp_path)
    assert result.returncode != 0
    assert "outcome mismatch: canonical-json-control-astral-and-key-order" in result.stderr


def test_default_utf16_key_sort_effect_mutation_fails_the_astral_key_corpus(tmp_path):
    node = _required_node()
    source = (ROOT / "tools/verify_interoperability_bundle.mjs").read_text()
    target = ".sort(compareCodePoints)"
    assert source.count(target) == 1
    mutated = source.replace(target, ".sort()")
    verifier = tmp_path / "verify-default-sort.mjs"
    verifier.write_text(mutated)
    environment = os.environ.copy()
    environment["AKASH_INTEROP_REPOSITORY_ROOT"] = str(ROOT)

    result = subprocess.run(
        [node, verifier],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "outcome mismatch: canonical-json-control-astral-and-key-order" in result.stderr
