import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "skills" / "governing-task-scope"
SCRIPT = SKILL_ROOT / "scripts" / "scope_guard.py"


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contract_digest(contract):
    return hashlib.sha256(canonical_bytes(contract)).hexdigest()


def base_contract():
    return {
        "schema_version": 1,
        "contract_id": "tc-test",
        "version": 1,
        "previous_digest": None,
        "state": "discovery",
        "objective": "Deliver the admitted behavior",
        "milestone": "First verifiable slice",
        "constraints": [
            {"id": "C-001", "source": "AGENTS.md#task-scope"},
        ],
        "milestone_constraint_ids": ["C-001"],
        "facts": [
            {
                "id": "F-001",
                "statement": "The public interface exists",
                "evidence": "src/api.py:10",
            }
        ],
        "assumptions": [
            {"id": "A-001", "statement": "One caller is sufficient", "status": "open"}
        ],
        "invariants": [
            {
                "id": "I-001",
                "statement": "Keep validation at the trust boundary",
                "verification": "python3 -m unittest",
            }
        ],
        "acceptance": [
            {
                "id": "AC-001",
                "statement": "The focused behavior passes",
                "verification": "python3 -m unittest",
                "status": "pending",
            }
        ],
        "scope": {
            "allowed": ["Implement the focused behavior"],
            "forbidden": ["Build a general platform"],
        },
        "decision": {
            "selected_path": "Use one standard-library script",
            "preserves": ["I-001"],
            "simplifications": [],
        },
        "work_items": [
            {"id": "W-001", "status": "unstarted", "constraint_ids": ["C-001"]}
        ],
        "budget": {"max_open_agents": 8, "max_cumulative_agents": 45},
        "usage": {"open_agents": 0, "cumulative_agents": 0},
        "findings": [],
        "artifacts": [],
        "evidence": [],
        "progress": {"phase": "discovery", "next_action": "Implement the first slice"},
    }


def next_contract(current):
    candidate = copy.deepcopy(current)
    candidate["version"] = current["version"] + 1
    candidate["previous_digest"] = contract_digest(current)
    return candidate


class ScopeGuardTests(unittest.TestCase):
    maxDiff = None

    def invoke(self, event, candidate, current=None, authority_text=None, write_output=True):
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            candidate_path = directory / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            command = [
                sys.executable,
                str(SCRIPT),
                "evaluate",
                "--event",
                event,
                "--candidate",
                str(candidate_path),
                "--json",
            ]
            if current is not None:
                current_path = directory / "current.json"
                current_path.write_text(json.dumps(current), encoding="utf-8")
                command.extend(["--current", str(current_path)])
            if authority_text is not None:
                authority_path = directory / "authority.toml"
                authority_path.write_text(authority_text, encoding="utf-8")
                command.extend(["--authority-index", str(authority_path)])
            output_path = directory / "approved.json"
            if write_output:
                command.extend(["--output", str(output_path)])

            completed = subprocess.run(
                command,
                cwd=directory,
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                body = json.loads(completed.stdout)
            except json.JSONDecodeError:
                body = {}
            output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else None
            output = json.loads(output_text) if output_text is not None else None
            return completed, body, output, output_text

    def assert_blocked(self, event, candidate, reason, current=None, authority_text=None):
        completed, body, output, _ = self.invoke(
            event, candidate, current=current, authority_text=authority_text
        )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(body.get("ok"), False)
        self.assertEqual(body.get("decision"), "block")
        self.assertIn(reason, body.get("reason_codes", []))
        self.assertIsNone(output)
        return body

    def test_task_start_writes_canonical_contract(self):
        contract = base_contract()
        completed, body, output, output_text = self.invoke("task-start", contract)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(body["decision"], "allow")
        self.assertEqual(body["contract_ref"]["digest"], contract_digest(contract))
        self.assertEqual(output, contract)
        self.assertEqual(
            output_text,
            json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n",
        )

    def test_malformed_contract_returns_typed_block_without_traceback(self):
        completed, body, output, _ = self.invoke("task-start", {})
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(body["decision"], "block")
        self.assertEqual(body["reason_codes"], ["contract_structure_invalid"])
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIsNone(output)

    def test_fact_requires_evidence_and_assumption_requires_status(self):
        no_evidence = base_contract()
        no_evidence["facts"][0]["evidence"] = ""
        self.assert_blocked("task-start", no_evidence, "fact_evidence_missing")

        bad_assumption = base_contract()
        bad_assumption["assumptions"][0]["status"] = "certain"
        self.assert_blocked("task-start", bad_assumption, "assumption_status_invalid")

    def test_simplification_requires_ceiling_and_trigger(self):
        missing_trigger = base_contract()
        missing_trigger["decision"]["simplifications"] = [
            {"id": "S-001", "summary": "Use a linear scan", "ceiling": "100 records"}
        ]
        self.assert_blocked(
            "task-start", missing_trigger, "simplification_metadata_missing"
        )

        complete = base_contract()
        complete["decision"]["simplifications"] = [
            {
                "id": "S-001",
                "summary": "Use a linear scan",
                "ceiling": "100 records",
                "trigger": "A measured input exceeds 100 records",
            }
        ]
        completed, body, _, _ = self.invoke("task-start", complete)
        self.assertEqual(completed.returncode, 0, body)
        self.assertEqual(body["decision"], "allow")

    def test_minimal_and_over_simplified_paths_preserve_every_invariant(self):
        minimal = base_contract()
        completed, body, _, _ = self.invoke("task-start", minimal)
        self.assertEqual(completed.returncode, 0, body)

        unsafe_shortcut = base_contract()
        unsafe_shortcut["decision"]["selected_path"] = "Skip validation"
        unsafe_shortcut["decision"]["preserves"] = []
        self.assert_blocked(
            "task-start", unsafe_shortcut, "invariant_not_preserved"
        )

    def test_stale_version_or_previous_digest_is_blocked(self):
        current = base_contract()
        stale_version = copy.deepcopy(current)
        stale_version["previous_digest"] = contract_digest(current)
        self.assert_blocked(
            "execution-contract", stale_version, "stale_contract", current=current
        )

        stale_digest = next_contract(current)
        stale_digest["previous_digest"] = "0" * 64
        self.assert_blocked(
            "execution-contract", stale_digest, "stale_contract", current=current
        )

    def test_execution_contract_preserves_discovery_objective(self):
        current = base_contract()
        admitted = next_contract(current)
        admitted["state"] = "execution"
        admitted["progress"]["phase"] = "execution"
        completed, body, _, _ = self.invoke(
            "execution-contract", admitted, current=current
        )
        self.assertEqual(completed.returncode, 0, body)

        drifted = copy.deepcopy(admitted)
        drifted["objective"] = "Build a different platform"
        self.assert_blocked(
            "execution-contract", drifted, "scope_expansion", current=current
        )

    def test_artifact_digest_mismatch_blocks_execution_proposal(self):
        current = base_contract()
        current["state"] = "execution"
        candidate = next_contract(current)
        with tempfile.TemporaryDirectory() as raw_dir:
            artifact = Path(raw_dir) / "plan.md"
            artifact.write_text("approved plan\n", encoding="utf-8")
            candidate["artifacts"] = [
                {
                    "kind": "plan",
                    "path": str(artifact),
                    "sha256": "0" * 64,
                }
            ]
            self.assert_blocked(
                "execution-proposal",
                candidate,
                "artifact_digest_mismatch",
                current=current,
            )

    def test_execution_proposal_removes_unrelated_unstarted_work(self):
        current = base_contract()
        current["state"] = "execution"
        current["constraints"].append(
            {"id": "C-002", "source": "docs/requirements.md#later"}
        )
        candidate = next_contract(current)
        candidate["work_items"].append(
            {"id": "W-002", "status": "unstarted", "constraint_ids": ["C-002"]}
        )

        completed, body, output, _ = self.invoke(
            "execution-proposal", candidate, current=current
        )
        self.assertEqual(completed.returncode, 0, body)
        self.assertEqual(body["decision"], "correct")
        self.assertEqual(body["reason_codes"], ["unrelated_unstarted_work_removed"])
        self.assertEqual([item["id"] for item in output["work_items"]], ["W-001"])

    def test_execution_proposal_cannot_expand_approved_scope(self):
        current = base_contract()
        current["state"] = "execution"
        candidate = next_contract(current)
        candidate["scope"]["allowed"].append("Build an unrelated dashboard")
        self.assert_blocked(
            "execution-proposal", candidate, "scope_expansion", current=current
        )

    def test_execution_proposal_blocks_unrelated_started_work(self):
        current = base_contract()
        current["state"] = "execution"
        current["constraints"].append(
            {"id": "C-002", "source": "docs/requirements.md#later"}
        )
        candidate = next_contract(current)
        candidate["work_items"].append(
            {"id": "W-002", "status": "in-progress", "constraint_ids": ["C-002"]}
        )
        self.assert_blocked(
            "execution-proposal", candidate, "scope_expansion", current=current
        )

    def test_delegation_budget_exhaustion_requires_handoff(self):
        current = base_contract()
        current["usage"]["open_agents"] = current["budget"]["max_open_agents"]
        event = self.delegation_event(current)
        body = self.assert_blocked(
            "delegation", event, "agent_budget_exceeded", current=current
        )
        self.assertIn("handoff_required", body["reason_codes"])

    def test_delegation_requires_current_contract_reference(self):
        current = base_contract()
        event = self.delegation_event(current)
        completed, body, output, _ = self.invoke(
            "delegation", event, current=current, write_output=False
        )
        self.assertEqual(completed.returncode, 0, body)
        self.assertEqual(body["decision"], "allow")
        self.assertEqual(body["contract_ref"], event["contract_ref"])
        self.assertIsNone(output)

    def test_stale_delegation_reference_is_blocked(self):
        current = base_contract()
        for field, value in (
            ("contract_id", "tc-old"),
            ("version", 99),
            ("digest", "0" * 64),
            ("milestone", "Old milestone"),
        ):
            with self.subTest(field=field):
                event = self.delegation_event(current)
                event["contract_ref"][field] = value
                self.assert_blocked(
                    "delegation",
                    event,
                    "stale_contract_reference",
                    current=current,
                )

    def test_unclassified_or_protected_deferred_finding_is_blocked(self):
        current = base_contract()
        current["state"] = "execution"

        unclassified = next_contract(current)
        unclassified["findings"] = [
            {"id": "R-001", "disposition": "defer", "evidence": "review.md:10"}
        ]
        self.assert_blocked(
            "review-disposition",
            unclassified,
            "finding_classification_missing",
            current=current,
        )

        protected = next_contract(current)
        protected["findings"] = [
            {
                "id": "R-001",
                "risk_category": "security",
                "disposition": "defer",
                "evidence": "review.md:10",
            }
        ]
        self.assert_blocked(
            "review-disposition",
            protected,
            "protected_finding_deferred",
            current=current,
        )

    def test_unrelated_non_protected_finding_may_be_deferred(self):
        current = base_contract()
        current["state"] = "execution"
        candidate = next_contract(current)
        candidate["findings"] = [
            {
                "id": "R-001",
                "risk_category": "ordinary",
                "disposition": "defer",
                "evidence": "review.md:10",
            }
        ]
        completed, body, _, _ = self.invoke(
            "review-disposition", candidate, current=current
        )
        self.assertEqual(completed.returncode, 0, body)
        self.assertEqual(body["decision"], "allow")

    def test_milestone_transition_requires_current_acceptance(self):
        current = base_contract()
        current["state"] = "execution"
        candidate = next_contract(current)
        candidate["milestone"] = "Second slice"
        self.assert_blocked(
            "milestone-transition",
            candidate,
            "acceptance_incomplete",
            current=current,
        )

    def test_completion_requires_all_acceptance_and_fresh_evidence(self):
        current = base_contract()
        current["state"] = "execution"

        incomplete = next_contract(current)
        incomplete["progress"]["phase"] = "complete"
        self.assert_blocked(
            "completion", incomplete, "acceptance_incomplete", current=current
        )

        no_evidence = next_contract(current)
        no_evidence["acceptance"][0]["status"] = "satisfied"
        no_evidence["progress"]["phase"] = "complete"
        self.assert_blocked(
            "completion", no_evidence, "verification_missing", current=current
        )

        complete = next_contract(current)
        complete["acceptance"][0]["status"] = "satisfied"
        complete["evidence"] = [
            {
                "id": "E-001",
                "kind": "implementation-verification",
                "statement": "Focused tests pass",
                "verification": "python3 -m unittest",
                "fresh": True,
            }
        ]
        complete["progress"]["phase"] = "complete"
        completed, body, _, _ = self.invoke("completion", complete, current=current)
        self.assertEqual(completed.returncode, 0, body)
        self.assertEqual(body["decision"], "allow")

    def test_optional_authority_index_resolves_constraint_ids(self):
        contract = base_contract()
        self.assert_blocked(
            "task-start",
            contract,
            "authority_missing",
            authority_text="[constraints]\n",
        )

        complete_index = (
            '[constraints."C-001"]\n'
            'source = "AGENTS.md#task-scope"\n'
        )
        completed, body, _, _ = self.invoke(
            "task-start", contract, authority_text=complete_index
        )
        self.assertEqual(completed.returncode, 0, body)

    def test_standalone_execution_has_no_superpowers_import(self):
        contract = base_contract()
        completed, body, _, _ = self.invoke("task-start", contract)
        self.assertEqual(completed.returncode, 0, body)
        self.assertNotIn("superpowers", SCRIPT.read_text(encoding="utf-8").lower())

    @staticmethod
    def delegation_event(current):
        return {
            "contract_ref": {
                "contract_id": current["contract_id"],
                "version": current["version"],
                "digest": contract_digest(current),
                "milestone": current["milestone"],
            },
            "work_item_ids": ["W-001"],
        }


if __name__ == "__main__":
    unittest.main()
