from pathlib import Path
import tempfile
import unittest
import json
from unittest import mock

from scripts.loopmem.errors import LoopMemoryError


CANONICAL_LONG = (
    "# Global Long-Term Memory\n"
    "\n"
    "## Methodology\n"
    "\n"
    "- [2026-08-14][verified] Keep methodology concise.\n"
    "  Evidence: design review\n"
    "\n"
    "## Fact Index\n"
    "\n"
    "- `~/loop-memory/global/facts/index.md`\n"
)
LEGACY_ENTRIES_LONG = (
    "# Global Long-Term Memory\n\n"
    "## Entries\n\n"
    "- [2026-08-14][verified] Legacy entry.\n"
    "  Evidence: fixture\n"
)
VERIFIED_FACT = (
    "- [2026-08-14][verified] The shared authority is under the user's home.\n"
    "  Evidence: installed enter acceptance\n"
)
INFERRED_FACT = (
    "- [2026-08-14][inferred] A provisional global observation.\n"
    "  Evidence: exploratory run\n"
)


class GlobalFactLayoutTests(unittest.TestCase):
    def module(self):
        from scripts.loopmem import global_facts

        return global_facts

    def test_canonical_layout_creates_index_and_detail_directories(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            module.ensure_facts_layout(root)

            self.assertEqual(
                (root / "global/facts/index.md").read_text(encoding="utf-8"),
                "# Global Fact Index\n\n## Entries\n",
            )
            for relative in (
                "global/facts/entries",
                "global/facts/history",
                "global/facts/receipts",
            ):
                self.assertTrue((root / relative).is_dir())

    def test_long_document_requires_methodology_and_exact_index_pointer(self):
        module = self.module()
        module.validate_long_document(CANONICAL_LONG)

        with self.assertRaises(LoopMemoryError) as context:
            module.validate_long_document(LEGACY_ENTRIES_LONG)

        self.assertEqual(context.exception.code, "global_long_not_canonical")

    def test_fact_promotion_writes_hashed_detail_and_summary_locator(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            result = module.promote_fact(root, VERIFIED_FACT)

            self.assertTrue(result["changed"])
            detail = Path(result["path"])
            self.assertTrue(detail.is_file())
            self.assertTrue(detail.name.startswith("f-"))
            self.assertEqual(detail.read_text(encoding="utf-8"), VERIFIED_FACT)
            index = (root / "global/facts/index.md").read_text(encoding="utf-8")
            self.assertIn("The shared authority is under the user's home.", index)
            self.assertIn(f"Detail: ~/loop-memory/global/facts/entries/{detail.name}", index)

            retry = module.promote_fact(root, VERIFIED_FACT)
            self.assertFalse(retry["changed"])
            self.assertEqual(retry["path"], str(detail))

    def test_fact_promotion_rejects_inferred_entries_before_creating_layout(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            with self.assertRaises(LoopMemoryError) as context:
                module.promote_fact(root, INFERRED_FACT)

            self.assertEqual(context.exception.code, "inferred_not_durable")
            self.assertFalse(root.exists())

    def test_fact_promotion_rejects_changed_content_at_existing_hash_path(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            first = module.promote_fact(root, VERIFIED_FACT)
            detail = Path(first["path"])
            detail.write_text("tampered\n", encoding="utf-8")

            with self.assertRaises(LoopMemoryError) as context:
                module.promote_fact(root, VERIFIED_FACT)

            self.assertEqual(context.exception.code, "global_fact_conflict")

    def test_organization_archives_exact_long_and_replaces_with_methodology(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            module.ensure_facts_layout(root)
            long_path = root / "global/long.md"
            long_path.write_text(LEGACY_ENTRIES_LONG, encoding="utf-8")

            result = module.organize_global_long(root, CANONICAL_LONG)

            self.assertTrue(result["changed"])
            self.assertEqual(long_path.read_text(encoding="utf-8"), CANONICAL_LONG)
            history = Path(result["history"])
            receipt = Path(result["receipt"])
            self.assertEqual(history.read_text(encoding="utf-8"), LEGACY_ENTRIES_LONG)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["history_path"], "global/facts/history/" + history.name)
            self.assertEqual(payload["history_sha256"], module.sha256_text(LEGACY_ENTRIES_LONG))
            self.assertEqual(payload["resulting_long_sha256"], module.sha256_text(CANONICAL_LONG))

            retry = module.organize_global_long(root, CANONICAL_LONG)
            self.assertFalse(retry["changed"])
            self.assertEqual(retry["history"], str(history))

    def test_organization_rolls_back_if_post_publish_evidence_fails(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "loop-memory"
            module.ensure_facts_layout(root)
            long_path = root / "global/long.md"
            long_path.write_text(LEGACY_ENTRIES_LONG, encoding="utf-8")

            with mock.patch.object(module, "read_json", return_value={}):
                with self.assertRaises(LoopMemoryError) as context:
                    module.organize_global_long(root, CANONICAL_LONG)

            self.assertEqual(context.exception.code, "global_organization_invalid")
            self.assertEqual(
                long_path.read_text(encoding="utf-8"),
                LEGACY_ENTRIES_LONG,
            )


if __name__ == "__main__":
    unittest.main()
