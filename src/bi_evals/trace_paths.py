"""Trace file naming helpers shared by the provider (writer) and scorer (reader).

The provider writes trace JSON to a per-(test, model, invocation) path so
that multi-model runs and repeat-N don't collide. The scorer must compute
the same test slug and model slug to find the trace it should grade.

These helpers used to live as private duplicates in `provider/entry.py` and
`scorer/entry.py`. Keeping them in lockstep was implicit, and they drifted
once the provider added `__{model}__{suffix}` to its filenames without the
scorer being updated — silently grading stale traces. Centralising the
naming here removes that drift risk.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def make_test_id_slug(prompt: str, vars_: dict[str, Any]) -> str:
    """Derive a filesystem-safe slug identifying a test.

    Prefers the `golden_file` test variable so the slug is stable across
    runs; falls back to an md5 of the prompt for ad-hoc tests.
    """
    golden_file = vars_.get("golden_file", "")
    test_id = golden_file if golden_file else hashlib.md5(prompt.encode()).hexdigest()
    return test_id.replace("/", "_").replace(".", "_")


def slugify_model(model: str) -> str:
    """Filesystem-safe model slug. Keeps letters, digits, dash; collapses others."""
    if not model:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9\-]+", "_", model).strip("_") or "unknown"
