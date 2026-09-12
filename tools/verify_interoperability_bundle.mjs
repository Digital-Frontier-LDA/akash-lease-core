#!/usr/bin/env node
// Independent stdlib-only verifier for the language-neutral golden bundle.

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const defaultRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const root = process.env.AKASH_INTEROP_REPOSITORY_ROOT ?? defaultRoot;
const bundleRoot = join(root, "interoperability", "akash-lifecycle-interoperability-v1");
const artifactPaths = new Set([
  "canonicalization.json",
  "coverage.json",
  "model.json",
  "vectors.json",
]);
const expectedModelDigest =
  "sha256:bc1fceb7c54215bc1acc178d54d291a0b77027a6d0dc50c78ec83d01a9aca739";
const expectedWireRoots = [
  "AdmissionRequest",
  "AdmissionResult",
  "PersistenceConfirmation",
  "CreatePermit",
  "PermitPresentation",
  "PermitRedemptionProposal",
  "CreateSubmissionAuthorization",
];
const expectedStorageRoots = [
  "AdmissionState",
  "ReservationProposal",
  "PermitRedemptionProposal",
];
const expectedMeasuredOutcomes = [
  "proposed",
  "active_deployment_limit",
  "reconcile_existing",
  "PersistenceConfirmationError",
];
const supportedRoles = new Set([
  "broker.admission_request",
  "broker.admission_result",
  "storage.capacity_reservation",
  "storage.permit_issuance",
  "storage.permit_redemption",
  "storage.prepared_create",
]);
const unavailableRoles = new Set([
  "broker.idempotency",
  "broker.typed_error",
  "chain.finality_observation",
  "runtime.io_adapter",
  "storage.allocation",
  "storage.create_journal",
  "storage.execution_close",
  "storage.financial_settlement",
  "storage.reconciliation",
  "storage.record_outcome",
  "transaction.atomic_membership",
]);
const expectedEnums = {
  AdmissionDisposition: ["proposed", "reconcile", "hold"],
  AdmissionReason: [
    "proposed",
    "reconcile_existing",
    "missing_required_budget",
    "stale_revision",
    "operation_conflict",
    "journal_unhealthy",
    "recovery_reader_unhealthy",
    "chain_census_unhealthy",
    "accounting_census_unhealthy",
    "census_not_current",
    "exposure_evidence_not_current",
    "active_deployment_limit",
    "unresolved_outcome_limit",
    "financial_exposure_limit",
    "exposure_policy_mismatch",
    "reservation_census_mismatch",
  ],
  BackendKind: ["self_managed", "mediated"],
  CensusStatus: ["verified", "incomplete", "unavailable"],
  OwnerEvidenceKind: ["verified_signer", "authenticated_mediator"],
  PermitRevocationStatus: ["active", "revoked"],
  ReservationState: [
    "reserved",
    "submitting",
    "create_outcome_unknown",
    "non_commit_proven",
    "committed",
    "execution_closed",
    "settled",
  ],
};

class LosslessJsonParser {
  constructor(text) {
    this.text = text;
    this.offset = 0;
  }

  parse() {
    const value = this.value();
    this.space();
    if (this.offset !== this.text.length) throw new Error("trailing JSON data");
    return value;
  }

  space() {
    while (/\s/.test(this.text[this.offset] ?? "")) this.offset += 1;
  }

  value() {
    this.space();
    const character = this.text[this.offset];
    if (character === '"') return this.string();
    if (character === "[") return this.array();
    if (character === "{") return this.object();
    for (const [token, value] of [
      ["true", true],
      ["false", false],
      ["null", null],
    ]) {
      if (this.text.startsWith(token, this.offset)) {
        this.offset += token.length;
        return value;
      }
    }
    const number = this.text.slice(this.offset).match(/^-?(?:0|[1-9][0-9]*)/);
    if (number === null) throw new Error(`invalid JSON at byte ${this.offset}`);
    this.offset += number[0].length;
    return BigInt(number[0]);
  }

  string() {
    const start = this.offset++;
    let escaped = false;
    while (this.offset < this.text.length) {
      const character = this.text[this.offset++];
      if (!escaped && character === '"') {
        const value = JSON.parse(this.text.slice(start, this.offset));
        assertUnicodeScalarString(value);
        return value;
      }
      if (!escaped && character === "\\") escaped = true;
      else escaped = false;
    }
    throw new Error("unterminated JSON string");
  }

  array() {
    const output = [];
    this.offset += 1;
    this.space();
    if (this.text[this.offset] === "]") {
      this.offset += 1;
      return output;
    }
    while (true) {
      output.push(this.value());
      this.space();
      const delimiter = this.text[this.offset++];
      if (delimiter === "]") return output;
      if (delimiter !== ",") throw new Error("invalid JSON array delimiter");
    }
  }

  object() {
    const output = Object.create(null);
    this.offset += 1;
    this.space();
    if (this.text[this.offset] === "}") {
      this.offset += 1;
      return output;
    }
    while (true) {
      this.space();
      if (this.text[this.offset] !== '"') throw new Error("JSON object key must be a string");
      const key = this.string();
      if (Object.hasOwn(output, key)) throw new Error(`duplicate JSON key: ${key}`);
      this.space();
      if (this.text[this.offset++] !== ":") throw new Error("missing JSON object colon");
      output[key] = this.value();
      this.space();
      const delimiter = this.text[this.offset++];
      if (delimiter === "}") return output;
      if (delimiter !== ",") throw new Error("invalid JSON object delimiter");
    }
  }
}

function assertUnicodeScalarString(value) {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const following = value.charCodeAt(index + 1);
      if (!(following >= 0xdc00 && following <= 0xdfff)) {
        throw new Error("JSON strings must contain only Unicode scalar values");
      }
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw new Error("JSON strings must contain only Unicode scalar values");
    }
  }
}

function parseJsonLossless(text) {
  return new LosslessJsonParser(text).parse();
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0));
  const rightPoints = Array.from(right, (character) => character.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

function canonical(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
  }
  if (typeof value === "bigint") return String(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object") {
    return `{${Object.keys(value)
      .sort(compareCodePoints)
      .map((key) => `${canonical(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  }
  throw new Error(`unsupported canonical value: ${typeof value}`);
}

function digestBytes(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function digest(value) {
  return digestBytes(Buffer.from(canonical(value), "utf8"));
}

const manifestRaw = await readFile(join(bundleRoot, "bundle.json"), "utf8");
const manifest = parseJsonLossless(manifestRaw);
if (
  manifest.contract !== "akash-lifecycle-interoperability/v1" ||
  manifest.bundle_format !== 1n ||
  manifest.core_model?.package !== "akash-lease-core" ||
  manifest.core_model?.version !== "0.12.0"
) {
  throw new Error("bundle identity mismatch");
}
let model;
let coverage;
let canonicalization;
for (const artifact of manifest.artifacts) {
  if (!artifactPaths.delete(artifact.path)) throw new Error(`locator mismatch: ${artifact.id}`);
  const raw = await readFile(join(bundleRoot, artifact.path), "utf8");
  const payload = parseJsonLossless(raw);
  if (canonical(payload) !== raw || digestBytes(Buffer.from(raw, "utf8")) !== artifact.digest) {
    throw new Error(`digest mismatch: ${artifact.path}`);
  }
  if (artifact.path === "model.json") model = payload;
  if (artifact.path === "coverage.json") coverage = payload;
  if (artifact.path === "canonicalization.json") canonicalization = payload;
}
if (artifactPaths.size !== 0) throw new Error("manifest artifact population mismatch");

if (
  digest(model) !== expectedModelDigest ||
  canonical(model.wire_roots) !== canonical(expectedWireRoots) ||
  canonical(model.storage_roots) !== canonical(expectedStorageRoots)
) {
  throw new Error("machine model semantic mismatch");
}

const expectedRoles = [...supportedRoles, ...unavailableRoles].sort();
if (
  coverage.normative_role_count !== BigInt(expectedRoles.length) ||
  canonical(coverage.normative_role_population) !== canonical(expectedRoles) ||
  canonical(Object.keys(coverage.roles).sort()) !== canonical(expectedRoles)
) {
  throw new Error("normative coverage role population mismatch");
}
if (canonical(coverage.measured_outcomes) !== canonical(expectedMeasuredOutcomes)) {
  throw new Error("coverage measured-outcome population mismatch");
}
for (const role of expectedRoles) {
  const expectedStatus = supportedRoles.has(role) ? "supported" : "unavailable_fail_closed";
  if (coverage.roles[role].status !== expectedStatus) {
    throw new Error(`coverage role status mismatch: ${role}`);
  }
  const explanation = coverage.roles[role].reason ?? coverage.roles[role].shape;
  if (typeof explanation !== "string" || explanation.trim() === "") {
    throw new Error(`coverage role reason mismatch: ${role}`);
  }
}
if (
  canonicalization.json_profile.object_key_order !== "ascending Unicode code point" ||
  canonicalization.json_profile.object_keys !==
    "Unicode scalar strings; typed record field names are ASCII" ||
  canonicalization.json_profile.strings !==
    "Unicode scalar strings only; lone surrogate code points rejected" ||
  canonicalization.json_profile.whitespace !== "none outside strings" ||
  canonicalization.json_profile.string_escaping !==
    "JSON with every non-ASCII code point escaped" ||
  canonicalization.json_profile.numbers !==
    "base-10 integers only; no leading zero; arbitrary precision" ||
  canonicalization.json_profile.duplicate_object_keys !== "reject" ||
  canonicalization.json_profile.terminal_newline !== false
) {
  throw new Error("canonicalization rule mismatch");
}
if (Object.keys(model.records).length !== 31 || Object.keys(model.enums).length !== 7) {
  throw new Error("machine model population mismatch");
}
for (const [name, values] of Object.entries(expectedEnums)) {
  if (canonical(model.enums[name]) !== canonical(values)) {
    throw new Error(`machine enum mismatch: ${name}`);
  }
}
for (const [name, record] of Object.entries(model.records)) {
  if (record.closed !== true || record.fields.some((field) => field.required !== true)) {
    throw new Error(`machine required-field mismatch: ${name}`);
  }
}

function validateType(value, expression) {
  if (typeof expression === "string") {
    const valid =
      (expression === "str" && typeof value === "string") ||
      (expression === "int" && typeof value === "bigint") ||
      (expression === "bool" && typeof value === "boolean") ||
      (expression === "null" && value === null);
    if (!valid) throw new Error(`expected ${expression}`);
    return;
  }
  if (expression.sequence !== undefined) {
    if (!Array.isArray(value)) throw new Error("expected sequence");
    for (const item of value) validateType(item, expression.sequence);
    return;
  }
  if (expression.enum !== undefined) {
    if (!model.enums[expression.enum].includes(value)) throw new Error(`invalid ${expression.enum}`);
    return;
  }
  if (expression.record !== undefined) {
    validateRecord(value, expression.record);
    return;
  }
  if (expression.one_of !== undefined) {
    const matches = expression.one_of.filter((candidate) => {
      try {
        validateType(value, candidate);
        return true;
      } catch {
        return false;
      }
    });
    if (matches.length !== 1) throw new Error("union value must match exactly one type");
    return;
  }
  throw new Error("unknown machine type expression");
}

function validateRecord(value, name) {
  const definition = model.records[name];
  if (definition === undefined || value?.$type !== name) throw new Error(`expected ${name}`);
  const expectedKeys = new Set(["$type", ...definition.fields.map((field) => field.name)]);
  for (const key of Object.keys(value)) {
    if (!expectedKeys.delete(key)) throw new Error(`unknown ${name} field: ${key}`);
  }
  if (expectedKeys.size !== 0) throw new Error(`missing ${name} fields`);
  for (const field of definition.fields) validateType(value[field.name], field.type);
}

function same(left, right) {
  return canonical(left) === canonical(right);
}

function isDigest(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function slots(reservation) {
  const activeStates = new Set(["reserved", "submitting", "create_outcome_unknown", "committed"]);
  const unresolvedStates = new Set(["reserved", "submitting", "create_outcome_unknown"]);
  return {
    active: activeStates.has(reservation.state) ? 1n : 0n,
    unresolved: unresolvedStates.has(reservation.state) ? 1n : 0n,
    exposure:
      reservation.state === "non_commit_proven" || reservation.state === "settled"
        ? 0n
        : reservation.request.exposure.amount_uact,
  };
}

function evaluateReserve(input) {
  validateRecord(input.state, "AdmissionState");
  validateRecord(input.request, "AdmissionRequest");
  const { state, request, now } = input;
  if (input.expected_revision !== state.revision) {
    return { disposition: "hold", reason: "stale_revision", proposal_revision: null };
  }
  const existing = state.reservations.find(
    (item) => item.request.prepared.operation_id === request.prepared.operation_id,
  );
  if (existing !== undefined) {
    return {
      disposition: same(existing.request, request) ? "reconcile" : "hold",
      reason: same(existing.request, request) ? "reconcile_existing" : "operation_conflict",
      proposal_revision: null,
    };
  }
  if (request.exposure.valid_until < now) {
    return { disposition: "hold", reason: "exposure_evidence_not_current", proposal_revision: null };
  }
  for (const budget of state.budgets) {
    for (const [field, reason] of [
      ["journal_status", "journal_unhealthy"],
      ["recovery_reader_status", "recovery_reader_unhealthy"],
      ["chain_status", "chain_census_unhealthy"],
      ["accounting_status", "accounting_census_unhealthy"],
    ]) {
      if (budget.census[field] !== "verified") {
        return { disposition: "hold", reason, proposal_revision: null };
      }
    }
    if (budget.census.valid_until < now || budget.limits.valid_until < now) {
      return { disposition: "hold", reason: "census_not_current", proposal_revision: null };
    }
    const usage = state.reservations.reduce(
      (sum, reservation) => {
        const value = slots(reservation);
        return {
          active: sum.active + value.active,
          unresolved: sum.unresolved + value.unresolved,
          exposure: sum.exposure + value.exposure,
        };
      },
      {
        active: budget.census.active_deployments,
        unresolved: budget.census.unresolved_create_outcomes,
        exposure: budget.census.financial_exposure_uact,
      },
    );
    if (usage.active + 1n > budget.limits.max_active_deployments) {
      return { disposition: "hold", reason: "active_deployment_limit", proposal_revision: null };
    }
    if (usage.unresolved + 1n > budget.limits.max_unresolved_create_outcomes) {
      return { disposition: "hold", reason: "unresolved_outcome_limit", proposal_revision: null };
    }
    if (usage.exposure + request.exposure.amount_uact > budget.limits.max_financial_exposure_uact) {
      return { disposition: "hold", reason: "financial_exposure_limit", proposal_revision: null };
    }
  }
  return {
    disposition: "proposed",
    reason: "proposed",
    proposal_revision: state.revision + 1n,
  };
}

function evaluateConfirmation(input) {
  validateRecord(input.proposal, "ReservationProposal");
  validateRecord(input.pre_state, "AdmissionState");
  validateRecord(input.request, "AdmissionRequest");
  validateRecord(input.observed_state, "AdmissionState");
  validateRecord(input.confirmation, "PersistenceConfirmation");
  if (
    !isDigest(input.proposal.expected_state_digest) ||
    !isDigest(input.proposal.proposed_state_digest) ||
    !isDigest(input.confirmation.persisted_state_digest) ||
    !isDigest(input.confirmation.evidence_digest) ||
    !isDigest(input.confirmation.authenticated_broker.authentication_evidence_digest)
  ) {
    return { accepted: false, core_exception: "ValueError", permit_issued: false };
  }
  const reservation = input.proposal.proposed_state.reservations.find(
    (item) => item.request.prepared.operation_id === input.proposal.operation_id,
  );
  const accepted =
    input.proposal.operation_id === input.request.prepared.operation_id &&
    input.proposal.expected_revision === input.pre_state.revision &&
    input.proposal.expected_state_digest === digest(input.pre_state).slice(7) &&
    input.proposal.proposed_revision === input.pre_state.revision + 1n &&
    input.proposal.proposed_state.revision === input.proposal.proposed_revision &&
    input.proposal.proposed_state_digest === digest(input.proposal.proposed_state).slice(7) &&
    same(input.observed_state, input.proposal.proposed_state) &&
    reservation !== undefined &&
    reservation.state === "reserved" &&
    same(reservation.request, input.request) &&
    reservation.state_evidence_digest === digest(input.request.prepared).slice(7) &&
    input.confirmation.operation_id === input.proposal.operation_id &&
    input.confirmation.persisted_revision === input.proposal.proposed_revision &&
    input.confirmation.persisted_state_digest === input.proposal.proposed_state_digest &&
    input.confirmation.persisted_at >= input.request.requested_at &&
    input.confirmation.authenticated_broker.verified_at <= input.confirmation.persisted_at &&
    input.confirmation.persisted_at <= input.confirmation.authenticated_broker.valid_until;
  return accepted
    ? { accepted: true, core_exception: null, permit_issued: true }
    : { accepted: false, core_exception: "PersistenceConfirmationError", permit_issued: false };
}

function evaluateCanonicalInteger(input) {
  const accepted = /^(?:0|[1-9][0-9]*)$/.test(input.decimal_text);
  if (accepted) {
    if (typeof input.value !== "bigint" || String(input.value) !== input.decimal_text) {
      throw new Error("integer vector value disagrees with decimal text");
    }
    return { accepted: true, canonical_json: canonical(input.value) };
  }
  if (Object.hasOwn(input, "value")) throw new Error("rejected integer vector must have no value");
  return { accepted: false, canonical_json: null };
}

function evaluateCanonicalEntries(input) {
  const value = Object.create(null);
  for (const entry of input.entries) {
    if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string") {
      throw new Error("canonical entry must be a key/value pair");
    }
    if (Object.hasOwn(value, entry[0])) throw new Error(`duplicate canonical key: ${entry[0]}`);
    value[entry[0]] = entry[1];
  }
  return { canonical_json: canonical(value) };
}

const vectorsRaw = await readFile(join(bundleRoot, "vectors.json"), "utf8");
const fixture = parseJsonLossless(vectorsRaw);
for (const vector of fixture.vectors) {
  const payload = { input: vector.input, expected: vector.expected };
  const bytes = Buffer.from(canonical(payload), "utf8");
  if (bytes.toString("hex") !== vector.canonical_payload_utf8_hex) {
    throw new Error(`canonical byte mismatch: ${vector.id}`);
  }
  if (digest(payload) !== vector.canonical_payload_sha256) {
    throw new Error(`canonical digest mismatch: ${vector.id}`);
  }
  let actual;
  if (vector.operation === "reserve_capacity") actual = evaluateReserve(vector.input);
  else if (vector.operation === "confirm_persisted_reservation") {
    actual = evaluateConfirmation(vector.input);
  } else if (vector.operation === "canonical_nonnegative_integer") {
    actual = evaluateCanonicalInteger(vector.input);
  } else if (vector.operation === "canonical_json_entries") {
    actual = evaluateCanonicalEntries(vector.input);
  } else throw new Error(`unsupported vector operation: ${vector.operation}`);
  if (canonical(actual) !== canonical(vector.expected)) {
    throw new Error(`outcome mismatch: ${vector.id}`);
  }
}

console.log(`verified ${fixture.vectors.length} vectors with independent Node adapter`);
