import dataclasses
import pickle
import unittest

from scripts.loopmem.errors import (
    EXIT_BLOCKED,
    EXIT_CORRUPT,
    EXIT_OK,
    EXIT_USAGE,
    LoopMemoryError,
)


class LoopMemoryErrorTests(unittest.TestCase):
    def test_public_error_contract(self):
        error = LoopMemoryError(code="needs_context", message="Context is missing")

        self.assertIsInstance(error, Exception)
        self.assertEqual(
            error.as_dict(),
            {
                "code": "needs_context",
                "message": "Context is missing",
                "recoverable": True,
            },
        )
        self.assertEqual((EXIT_OK, EXIT_USAGE, EXIT_BLOCKED, EXIT_CORRUPT), (0, 2, 3, 4))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            error.code = "changed"

    def test_normal_exception_and_pickle_semantics(self):
        error = LoopMemoryError(
            code="blocked",
            message="Operation is blocked",
            recoverable=False,
        )

        self.assertEqual(str(error), "Operation is blocked")
        self.assertEqual(error.args, ("Operation is blocked",))
        self.assertIs(error.recoverable, False)

        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual(restored.code, "blocked")
        self.assertEqual(restored.message, "Operation is blocked")
        self.assertIs(restored.recoverable, False)
        self.assertEqual(str(restored), "Operation is blocked")


if __name__ == "__main__":
    unittest.main()
