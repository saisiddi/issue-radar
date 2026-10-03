from __future__ import annotations

"""Deterministic, rule-based issue triage. Implemented in step 3.

No LLM is used for status or fit_tag decisions.
"""


def triage_issue(issue: dict, comments: list[dict], timeline: list[dict], config) -> dict:
    raise NotImplementedError("implemented in step 3")
