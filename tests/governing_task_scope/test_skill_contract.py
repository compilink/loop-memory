from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "skills" / "governing-task-scope"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_PATH = SKILL_ROOT / "agents" / "openai.yaml"
GLOBAL_AGENTS = REPO_ROOT / "global" / "AGENTS.loop-engineering.md"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.skill_lower = cls.skill.lower()
        cls.skill_flat = re.sub(r"\s+", " ", cls.skill_lower)
        cls.openai = OPENAI_PATH.read_text(encoding="utf-8")
        cls.global_agents = GLOBAL_AGENTS.read_text(encoding="utf-8")

    def test_skill_uses_first_principles_before_occam(self):
        first = self.skill_lower.find("first principles")
        occam = self.skill_lower.find("occam")
        self.assertGreaterEqual(first, 0)
        self.assertGreaterEqual(occam, 0)
        self.assertLess(first, occam)
        for phrase in ("verified facts", "assumptions", "invariants", "acceptance"):
            self.assertIn(phrase, self.skill_lower)

    def test_skill_rejects_over_simplification_and_assumption_as_fact(self):
        self.assertIn("simplicity is not evidence", self.skill_flat)
        self.assertIn("never convert an assumption into a fact", self.skill_flat)
        self.assertIn("security", self.skill_lower)
        self.assertIn("data-integrity", self.skill_lower)
        self.assertIn("compatibility", self.skill_lower)

    def test_skill_records_simplification_ceiling_and_trigger(self):
        self.assertIn("simplifications", self.skill)
        self.assertIn("ceiling", self.skill_lower)
        self.assertIn("reconsideration trigger", self.skill_lower)
        self.assertIn("not a separate debt", self.skill_lower)

    def test_skill_requires_solution_ladder_and_scope_delta_stop(self):
        for phrase in (
            "existing implementation",
            "standard library",
            "platform-native",
            "installed dependency",
            "acceptance",
            "verified root cause",
            "stop",
            "new contract version",
        ):
            self.assertIn(phrase, self.skill_flat)

    def test_global_agents_keeps_solution_ladder_and_reopen_rule_thin(self):
        flat = re.sub(r"\s+", " ", self.global_agents.lower())
        for phrase in (
            "existing implementation",
            "standard library",
            "platform-native",
            "installed dependency",
            "stop",
            "new contract version",
        ):
            self.assertIn(phrase, flat)

    def test_global_agents_has_no_wrapped_paragraph_lines(self):
        self.assertFalse(
            [line for line in self.global_agents.splitlines() if line.startswith((" ", "\t"))]
        )

    def test_global_agents_and_long_memory_have_distinct_roles(self):
        methodology = (REPO_ROOT / "global" / "global-long-methodology.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Evidence:", self.global_agents)
        self.assertNotIn("[verified]", self.global_agents)
        self.assertIn("compact executable trigger layer", methodology)
        self.assertIn("durable rationale and method summaries", methodology)

    def test_skill_uses_enter_returned_status_and_public_session_write(self):
        for phrase in (
            "managing-loop-memory",
            "enter",
            "returned `status`",
            "session-write",
            "--kind status",
        ):
            self.assertIn(phrase, self.skill)
        self.assertIn("do not edit loop memory internals", self.skill_flat)

    def test_skill_requires_subagent_contract_reentry(self):
        for phrase in (
            "subagent",
            "re-enter",
            "inbox",
            "contract_id",
            "version",
            "digest",
            "milestone",
        ):
            self.assertIn(phrase, self.skill_lower)
        self.assertIn("chat summary", self.skill_flat)

    def test_skill_calls_guard_at_every_published_boundary(self):
        for event in (
            "task-start",
            "execution-contract",
            "execution-proposal",
            "delegation",
            "review-disposition",
            "milestone-transition",
            "completion",
        ):
            self.assertIn(event, self.skill)
        self.assertIn("scope_guard.py evaluate", self.skill)

    def test_skill_works_without_superpowers(self):
        self.assertIn("workflow-neutral", self.skill_lower)
        self.assertIn("does not require superpowers", self.skill_flat)

    def test_superpowers_is_described_only_as_optional_adapter(self):
        self.assertIn("optional adapter", self.skill_lower)
        self.assertNotIn("required sub-skill", self.skill_lower)

    def test_global_agents_is_thin_trigger(self):
        match = re.search(
            r"^### Task Scope Governance\n(?P<body>.*?)(?=^### |\Z)",
            self.global_agents,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        section = match.group("body")
        bullets = [line for line in section.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(bullets), 5)
        self.assertIn("`governing-task-scope`", section)
        self.assertIn("workflowneutral", re.sub(r"[^a-z]", "", section.lower()))
        for forbidden in ("schema_version", "scope_guard.py", "contract_ref", "ponytail"):
            self.assertNotIn(forbidden, section.lower())

    def test_openai_yaml_declares_no_tool_dependencies(self):
        self.assertIn("interface:", self.openai)
        for key in ("tools:", "mcp_servers:", "dependencies:", "superpowers"):
            self.assertNotIn(key, self.openai.lower())

    def test_skill_stays_under_180_lines(self):
        self.assertLessEqual(len(self.skill.splitlines()), 180)


if __name__ == "__main__":
    unittest.main()
