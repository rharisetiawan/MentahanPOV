"""Small credential-comparison helpers shared by the dashboards and the worker."""

from __future__ import annotations

import secrets


def safe_equal(a: object, b: object) -> bool:
    """Constant-time string equality; non-str / None never match.

    ``secrets.compare_digest`` raises on non-ASCII ``str``, so compare bytes.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
