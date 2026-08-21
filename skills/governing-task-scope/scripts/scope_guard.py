#!/usr/bin/env python3
"""Deterministic lifecycle gate for versioned task contracts."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib


SCHEMA_VERSION = 1
CONTRACT_EVENTS = {
    "task-start",
    "execution-contract",
    "execution-proposal",
    "review-disposition",
    "milestone-transition",
    "completion",
}
EVENTS = CONTRACT_EVENTS | {"delegation"}
PROTECTED_RISKS = {"security", "data-integrity", "compatibility"}
RISK_CATEGORIES = PROTECTED_RISKS | {"ordinary"}
DISPOSITIONS = {"block", "fix-now", "defer"}
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
CONTRACT_FIELDS = {
    "schema_version",
    "contract_id",
    "version",
    "previous_digest",
    "state",
    "objective",
    "milestone",
    "constraints",
    "milestone_constraint_ids",
    "facts",
    "assumptions",
    "invariants",
    "acceptance",
    "scope",
    "decision",
    "work_items",
    "budget",
    "usage",
    "findings",
    "artifacts",
    "evidence",
    "progress",
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_contract(contract):
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def load_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _string_list(value, allow_empty=True):
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_nonempty(item) for item in value)
        and len(value) == len(set(value))
    )


def _object_list(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _ids(entries):
    return [entry.get("id") for entry in entries] if _object_list(entries) else []


def _valid_unique_ids(entries):
    ids = _ids(entries)
    return all(_nonempty(item) for item in ids) and len(ids) == len(set(ids))


def validate_contract(contract, authority_index=None):
    reasons = set()
    if set(contract) != CONTRACT_FIELDS:
        reasons.add("contract_structure_invalid")
        return reasons
    if contract.get("schema_version") != SCHEMA_VERSION:
        reasons.add("schema_version_unsupported")
    if not _nonempty(contract.get("contract_id")) or not _positive_int(
        contract.get("version")
    ):
        reasons.add("contract_structure_invalid")
    previous = contract.get("previous_digest")
    if previous is not None and (
        not isinstance(previous, str) or not HEX_DIGEST.fullmatch(previous)
    ):
        reasons.add("contract_structure_invalid")
    if contract.get("state") not in {"discovery", "execution"}:
        reasons.add("contract_structure_invalid")
    if not _nonempty(contract.get("objective")) or not _nonempty(
        contract.get("milestone")
    ):
        reasons.add("contract_structure_invalid")

    constraints = contract.get("constraints")
    if not _object_list(constraints) or not constraints or not _valid_unique_ids(constraints):
        reasons.add("contract_structure_invalid")
        constraint_ids = set()
    else:
        constraint_ids = set(_ids(constraints))
        if any(not _nonempty(item.get("source")) for item in constraints):
            reasons.add("authority_missing")
    milestone_ids = contract.get("milestone_constraint_ids")
    if (
        not _string_list(milestone_ids, allow_empty=False)
        or not set(milestone_ids).issubset(constraint_ids)
    ):
        reasons.add("authority_missing")

    facts = contract.get("facts")
    if not _object_list(facts) or not _valid_unique_ids(facts):
        reasons.add("contract_structure_invalid")
    else:
        for fact in facts:
            if not _nonempty(fact.get("statement")):
                reasons.add("contract_structure_invalid")
            if not _nonempty(fact.get("evidence")):
                reasons.add("fact_evidence_missing")

    assumptions = contract.get("assumptions")
    if not _object_list(assumptions) or not _valid_unique_ids(assumptions):
        reasons.add("contract_structure_invalid")
    else:
        for assumption in assumptions:
            if not _nonempty(assumption.get("statement")):
                reasons.add("contract_structure_invalid")
            if assumption.get("status") != "open":
                reasons.add("assumption_status_invalid")

    invariants = contract.get("invariants")
    if (
        not _object_list(invariants)
        or not invariants
        or not _valid_unique_ids(invariants)
    ):
        reasons.add("contract_structure_invalid")
        invariant_ids = set()
    else:
        invariant_ids = set(_ids(invariants))
        if any(
            not _nonempty(item.get("statement"))
            or not _nonempty(item.get("verification"))
            for item in invariants
        ):
            reasons.add("contract_structure_invalid")

    acceptance = contract.get("acceptance")
    if (
        not _object_list(acceptance)
        or not acceptance
        or not _valid_unique_ids(acceptance)
    ):
        reasons.add("contract_structure_invalid")
    else:
        for item in acceptance:
            if (
                not _nonempty(item.get("statement"))
                or not _nonempty(item.get("verification"))
                or item.get("status") not in {"pending", "satisfied"}
            ):
                reasons.add("contract_structure_invalid")

    scope = contract.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"allowed", "forbidden"}:
        reasons.add("contract_structure_invalid")
    elif not _string_list(scope.get("allowed"), allow_empty=False) or not _string_list(
        scope.get("forbidden")
    ):
        reasons.add("contract_structure_invalid")

    decision = contract.get("decision")
    if not isinstance(decision, dict) or set(decision) != {
        "selected_path",
        "preserves",
        "simplifications",
    }:
        reasons.add("contract_structure_invalid")
    else:
        if not _nonempty(decision.get("selected_path")):
            reasons.add("contract_structure_invalid")
        preserves = decision.get("preserves")
        if not _string_list(preserves) or set(preserves) != invariant_ids:
            reasons.add("invariant_not_preserved")
        simplifications = decision.get("simplifications")
        if not _object_list(simplifications) or not _valid_unique_ids(simplifications):
            reasons.add("simplification_metadata_missing")
        else:
            for item in simplifications:
                if any(
                    not _nonempty(item.get(field))
                    for field in ("summary", "ceiling", "trigger")
                ):
                    reasons.add("simplification_metadata_missing")

    work_items = contract.get("work_items")
    if not _object_list(work_items) or not _valid_unique_ids(work_items):
        reasons.add("contract_structure_invalid")
    else:
        for item in work_items:
            if item.get("status") not in {
                "unstarted",
                "in-progress",
                "completed",
                "deferred",
            }:
                reasons.add("contract_structure_invalid")
            item_constraints = item.get("constraint_ids")
            if not _string_list(item_constraints, allow_empty=False) or not set(
                item_constraints
            ).issubset(constraint_ids):
                reasons.add("authority_missing")

    budget = contract.get("budget")
    usage = contract.get("usage")
    if (
        not isinstance(budget, dict)
        or set(budget) != {"max_open_agents", "max_cumulative_agents"}
        or not all(_positive_int(value) for value in budget.values())
        or budget["max_open_agents"] > budget["max_cumulative_agents"]
    ):
        reasons.add("contract_structure_invalid")
    if (
        not isinstance(usage, dict)
        or set(usage) != {"open_agents", "cumulative_agents"}
        or not all(_nonnegative_int(value) for value in usage.values())
    ):
        reasons.add("contract_structure_invalid")

    findings = contract.get("findings")
    if not _object_list(findings) or not _valid_unique_ids(findings):
        reasons.add("contract_structure_invalid")
    else:
        for finding in findings:
            risk = finding.get("risk_category")
            disposition = finding.get("disposition")
            if risk not in RISK_CATEGORIES or disposition not in DISPOSITIONS:
                reasons.add("finding_classification_missing")
            if risk in PROTECTED_RISKS and disposition == "defer":
                reasons.add("protected_finding_deferred")
            if not _nonempty(finding.get("evidence")):
                reasons.add("contract_structure_invalid")

    artifacts = contract.get("artifacts")
    if not _object_list(artifacts):
        reasons.add("contract_structure_invalid")
    else:
        for artifact in artifacts:
            allowed = {"kind", "path", "sha256", "adapter"}
            if (
                not set(artifact).issubset(allowed)
                or not {"kind", "path", "sha256"}.issubset(artifact)
                or not _nonempty(artifact.get("kind"))
                or not _nonempty(artifact.get("path"))
                or not isinstance(artifact.get("sha256"), str)
                or not HEX_DIGEST.fullmatch(artifact["sha256"])
                or ("adapter" in artifact and not _nonempty(artifact["adapter"]))
            ):
                reasons.add("contract_structure_invalid")

    evidence = contract.get("evidence")
    if not _object_list(evidence) or not _valid_unique_ids(evidence):
        reasons.add("contract_structure_invalid")
    else:
        for item in evidence:
            if (
                not _nonempty(item.get("kind"))
                or not _nonempty(item.get("statement"))
                or not _nonempty(item.get("verification"))
                or not isinstance(item.get("fresh"), bool)
            ):
                reasons.add("contract_structure_invalid")

    progress = contract.get("progress")
    if (
        not isinstance(progress, dict)
        or set(progress) != {"phase", "next_action"}
        or progress.get("phase") not in {"discovery", "execution", "complete"}
        or not _nonempty(progress.get("next_action"))
    ):
        reasons.add("contract_structure_invalid")

    if authority_index is not None:
        indexed = authority_index.get("constraints", {})
        if not isinstance(indexed, dict):
            reasons.add("authority_missing")
        else:
            for constraint in constraints if _object_list(constraints) else []:
                entry = indexed.get(constraint.get("id"))
                if not isinstance(entry, dict) or entry.get("source") != constraint.get(
                    "source"
                ):
                    reasons.add("authority_missing")
    return reasons


def _validate_artifacts(contract):
    reasons = set()
    for artifact in contract.get("artifacts", []):
        path = Path(artifact["path"])
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != artifact["sha256"]:
            reasons.add("artifact_digest_mismatch")
    return reasons


def _same_material_contract(current, candidate):
    fields = ("contract_id", "objective", "constraints", "invariants")
    return all(candidate.get(field) == current.get(field) for field in fields)


def validate_transition(event, candidate, current):
    reasons = set()
    if event == "task-start":
        if current is not None or candidate.get("version") != 1 or candidate.get(
            "previous_digest"
        ) is not None:
            reasons.add("stale_contract")
        return reasons
    if current is None:
        return {"current_contract_missing"}
    if (
        candidate.get("contract_id") != current.get("contract_id")
        or candidate.get("version") != current.get("version") + 1
        or candidate.get("previous_digest") != digest_contract(current)
    ):
        reasons.add("stale_contract")
    if event == "execution-contract":
        if candidate.get("state") != "execution":
            reasons.add("illegal_transition")
        if not _same_material_contract(current, candidate):
            reasons.add("scope_expansion")
        return reasons
    if not _same_material_contract(current, candidate):
        reasons.add("scope_expansion")
    current_allowed = set(current.get("scope", {}).get("allowed", []))
    candidate_allowed = set(candidate.get("scope", {}).get("allowed", []))
    current_forbidden = set(current.get("scope", {}).get("forbidden", []))
    candidate_forbidden = set(candidate.get("scope", {}).get("forbidden", []))
    if not candidate_allowed.issubset(current_allowed) or not current_forbidden.issubset(
        candidate_forbidden
    ):
        reasons.add("scope_expansion")
    if event == "execution-proposal":
        admitted = set(candidate.get("milestone_constraint_ids", []))
        if any(
            item.get("status") != "unstarted"
            and not (set(item.get("constraint_ids", [])) & admitted)
            for item in candidate.get("work_items", [])
        ):
            reasons.add("scope_expansion")
    if event == "milestone-transition" and any(
        item.get("status") != "satisfied" for item in current.get("acceptance", [])
    ):
        reasons.add("acceptance_incomplete")
    if event == "completion":
        if any(
            item.get("status") != "satisfied"
            for item in candidate.get("acceptance", [])
        ):
            reasons.add("acceptance_incomplete")
        fresh = any(
            item.get("kind") == "implementation-verification" and item.get("fresh") is True
            for item in candidate.get("evidence", [])
        )
        if not fresh:
            reasons.add("verification_missing")
    return reasons


def apply_safe_corrections(event, candidate):
    corrected = copy.deepcopy(candidate)
    reason_codes = set()
    if event != "execution-proposal":
        return corrected, reason_codes
    admitted = set(corrected["milestone_constraint_ids"])
    kept = []
    for item in corrected["work_items"]:
        related = bool(set(item["constraint_ids"]) & admitted)
        if item["status"] == "unstarted" and not related:
            reason_codes.add("unrelated_unstarted_work_removed")
        else:
            kept.append(item)
    corrected["work_items"] = kept
    return corrected, reason_codes


def _contract_ref(contract):
    return {
        "contract_id": contract["contract_id"],
        "version": contract["version"],
        "digest": digest_contract(contract),
        "milestone": contract["milestone"],
    }


def _evaluate_delegation(candidate, current, authority_index):
    reasons = set()
    if current is None:
        reasons.add("current_contract_missing")
        return None, reasons
    reasons.update(validate_contract(current, authority_index))
    if set(candidate) != {"contract_ref", "work_item_ids"} or not isinstance(
        candidate.get("contract_ref"), dict
    ):
        reasons.add("contract_structure_invalid")
        return _contract_ref(current), reasons
    expected = _contract_ref(current)
    if candidate["contract_ref"] != expected:
        reasons.add("stale_contract_reference")
    work_item_ids = candidate.get("work_item_ids")
    if not _string_list(work_item_ids, allow_empty=False):
        reasons.add("delegation_scope_mismatch")
    else:
        by_id = {item["id"]: item for item in current.get("work_items", [])}
        admitted = set(current.get("milestone_constraint_ids", []))
        for item_id in work_item_ids:
            item = by_id.get(item_id)
            if item is None or not (set(item.get("constraint_ids", [])) & admitted):
                reasons.add("delegation_scope_mismatch")
    budget = current.get("budget", {})
    usage = current.get("usage", {})
    if usage.get("open_agents", 0) >= budget.get("max_open_agents", 0) or usage.get(
        "cumulative_agents", 0
    ) >= budget.get("max_cumulative_agents", 0):
        reasons.update({"agent_budget_exceeded", "handoff_required"})
    return expected, reasons


def evaluate(event, candidate, current=None, authority_index=None):
    if event == "delegation":
        reference, reasons = _evaluate_delegation(candidate, current, authority_index)
        return candidate, reference, reasons, set()

    reasons = validate_contract(candidate, authority_index)
    if "contract_structure_invalid" in reasons:
        return candidate, None, reasons, set()
    if current is not None:
        reasons.update(validate_contract(current, authority_index))
    reasons.update(validate_transition(event, candidate, current))
    if event in {"execution-contract", "execution-proposal", "completion"}:
        reasons.update(_validate_artifacts(candidate))
    corrected, correction_codes = apply_safe_corrections(event, candidate)
    return corrected, _contract_ref(corrected), reasons, correction_codes


def _write_contract(path, contract):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(canonical_json(contract) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def _result(event, decision, reason_codes, contract_ref=None):
    result = {
        "ok": decision != "block",
        "operation": "evaluate",
        "event": event,
        "decision": decision,
        "reason_codes": sorted(reason_codes),
    }
    if contract_ref is not None:
        result["contract_ref"] = contract_ref
    return result


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate_parser = subparsers.add_parser("evaluate", help="evaluate a lifecycle event")
    evaluate_parser.add_argument("--event", choices=sorted(EVENTS), required=True)
    evaluate_parser.add_argument("--candidate", required=True)
    evaluate_parser.add_argument("--current")
    evaluate_parser.add_argument("--authority-index")
    evaluate_parser.add_argument("--output")
    evaluate_parser.add_argument("--json", action="store_true", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        candidate = load_object(args.candidate)
        current = load_object(args.current) if args.current else None
        authority = None
        if args.authority_index:
            with Path(args.authority_index).open("rb") as handle:
                authority = tomllib.load(handle)
        approved, reference, reasons, corrections = evaluate(
            args.event, candidate, current=current, authority_index=authority
        )
        if reasons:
            print(canonical_json(_result(args.event, "block", reasons, reference)))
            return 3
        decision = "correct" if corrections else "allow"
        if args.event in CONTRACT_EVENTS and args.output:
            _write_contract(args.output, approved)
        print(canonical_json(_result(args.event, decision, corrections, reference)))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        print(canonical_json(_result(args.event, "block", {"contract_io_error"})))
        return 4


if __name__ == "__main__":
    sys.exit(main())
