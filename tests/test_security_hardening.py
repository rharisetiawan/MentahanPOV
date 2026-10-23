"""Constant-time auth comparison and .env injection guards."""

from __future__ import annotations

import unittest

from admin import env_schema, env_store
from core.authutil import safe_equal


class SafeEqualTests(unittest.TestCase):
    def test_matches_and_mismatches(self) -> None:
        self.assertTrue(safe_equal("abc", "abc"))
        self.assertFalse(safe_equal("abc", "abd"))
        self.assertTrue(safe_equal("é", "é"))  # non-ASCII must not raise

    def test_none_never_matches(self) -> None:
        self.assertFalse(safe_equal(None, ""))
        self.assertFalse(safe_equal("", None))


class EnvInjectionTests(unittest.TestCase):
    def test_newline_in_value_cannot_add_keys(self) -> None:
        sections = [
            env_schema.Section(
                title="t",
                fields=[env_schema.Field(key="A", default="", comment="", kind="text")],
            )
        ]
        out = env_store.render(sections, {"A": "x\nEVIL=1", "B": "y\r\nZ=2"})
        keys = [ln.split("=", 1)[0] for ln in out.splitlines() if "=" in ln and not ln.startswith("#")]
        self.assertEqual(keys, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
