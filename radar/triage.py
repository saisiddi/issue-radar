from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# GitHub's own auto-close keywords: close(s/d), fix(es/ed), resolve(s/d),
# optionally qualified with "owner/repo#N" for a cross-repo reference.
CLOSING_KEYWORD_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*(?:[\w.-]+/[\w.-]+)?#(\d+)\b"
)


def _closing_keyword_targets(body: str | None) -> set[int]:
    if not body:
        return set()
    return {int(n) for n in CLOSING_KEYWORD_RE.findall(body)}


@dataclass
class PRReference:
    number: int | None
    url: str
    repo: str
    merged: bool
    open: bool
    same_repo: bool
    closes_this_issue: bool


@dataclass
class LinkedPRResult:
    has_linked_pr: bool
    unsure: bool
    notes: list[str] = field(default_factory=list)


def _extract_pr_references(timeline: list[dict], issue_repo: str, issue_number: int) -> list[PRReference]:
    """Pull PR references out of cross-referenced timeline events.

    `connected` events (an explicit sidebar link, no closing keyword) carry
    no target field over REST, so a manually-linked PR without a closing
    keyword in its body is invisible to this detector. That gap is covered
    by falling back to UNSURE rather than ever claiming "free" on weak
    evidence - see analyze_linked_prs.
    """
    refs = []
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source = event.get("source") or {}
        src_issue = source.get("issue") or {}
        pr_info = src_issue.get("pull_request")
        if not pr_info:
            continue  # a plain issue mentioned this one, not a PR

        repo_full_name = (src_issue.get("repository") or {}).get("full_name") or ""
        refs.append(
            PRReference(
                number=src_issue.get("number"),
                url=src_issue.get("html_url") or pr_info.get("html_url") or "",
                repo=repo_full_name or "unknown",
                merged=bool(pr_info.get("merged_at")),
                open=src_issue.get("state") == "open",
                same_repo=repo_full_name.lower() == issue_repo.lower(),
                closes_this_issue=issue_number in _closing_keyword_targets(src_issue.get("body")),
            )
        )
    return refs


def analyze_linked_prs(timeline: list[dict], issue_repo: str, issue_number: int) -> LinkedPRResult:
    refs = _extract_pr_references(timeline, issue_repo, issue_number)
    has_pr = False
    unsure = False
    notes: list[str] = []

    for ref in refs:
        if not ref.same_repo:
            unsure = True
            notes.append(f"possible work in progress elsewhere: PR #{ref.number} in {ref.repo} ({ref.url})")
            continue

        if ref.merged or ref.open:
            state_word = "merged" if ref.merged else "open"
            if ref.closes_this_issue:
                has_pr = True
                notes.append(f"PR #{ref.number} ({state_word}) references closing this issue")
            else:
                unsure = True
                notes.append(f"PR #{ref.number} ({state_word}) mentions this issue but has no closing keyword")
            continue

        # Same repo, neither open nor merged: closed without merging.
        notes.append(f"previous attempt #{ref.number} closed unmerged")

    return LinkedPRResult(has_linked_pr=has_pr, unsure=unsure and not has_pr, notes=notes)


def count_claim_comments(comments: list[dict], claim_phrases: list[str]) -> int:
    count = 0
    for comment in comments:
        body = (comment.get("body") or "").lower()
        if any(phrase in body for phrase in claim_phrases):
            count += 1
    return count


def _label_names(labels: list) -> list[str]:
    return [(label.get("name") if isinstance(label, dict) else str(label)).lower() for label in labels]


def matched_reserved_labels(labels: list, reserved_labels: list[str]) -> list[str]:
    names = _label_names(labels)
    return [name for name in names if name in reserved_labels]


def compute_fit(issue: dict, positive_keywords: list[str], negative_keywords: list[str]) -> tuple[str, list[str]]:
    haystack = " ".join(
        _label_names(issue.get("labels", []))
        + [(issue.get("title") or "").lower(), (issue.get("body") or "").lower()]
    )

    matched_positive = [k for k in positive_keywords if k in haystack]
    matched_negative = [k for k in negative_keywords if k in haystack]

    if matched_negative and not matched_positive:
        return "SKIP", matched_negative
    if matched_positive and not matched_negative:
        return "GOOD", matched_positive
    if matched_positive and matched_negative:
        return "MAYBE", matched_positive + [f"-{k}" for k in matched_negative]
    return "MAYBE", []


def days_since(timestamp: str | None, now: datetime | None = None) -> int | None:
    if not timestamp:
        return None
    now = now or datetime.now(timezone.utc)
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (now - dt).days


def compute_status(assigned: bool, linked: LinkedPRResult, contested: bool, reserved: bool) -> str:
    if linked.unsure:
        return "UNSURE"
    if linked.has_linked_pr:
        return "HAS-PR"
    if reserved:
        return "DISCUSS-ONLY"
    if contested:
        return "CONTESTED"
    if assigned:
        return "CLAIMED"
    return "OPEN-FREE"


def triage_issue(
    issue: dict,
    comments: list[dict],
    timeline: list[dict],
    repo_name: str,
    positive_keywords: list[str],
    negative_keywords: list[str],
    claim_phrases: list[str],
    reserved_labels: list[str],
    now: datetime | None = None,
) -> dict:
    assigned = bool(issue.get("assignees"))
    linked = analyze_linked_prs(timeline, repo_name, issue["number"])
    claim_count = count_claim_comments(comments, claim_phrases)
    contested = claim_count >= 2 and not assigned
    fit_tag, matched_keywords = compute_fit(issue, positive_keywords, negative_keywords)
    reserved_matches = matched_reserved_labels(issue.get("labels", []), reserved_labels)
    status = compute_status(assigned, linked, contested, bool(reserved_matches))

    return {
        "number": issue["number"],
        "title": issue.get("title"),
        "assigned": assigned,
        "has_linked_pr": linked.has_linked_pr,
        "linked_pr_unsure": linked.unsure,
        "linked_pr_notes": linked.notes,
        "claim_comments": claim_count,
        "contested": contested,
        "fit_tag": fit_tag,
        "matched_keywords": matched_keywords,
        "staleness_days": days_since(issue.get("updated_at"), now=now),
        "reserved": bool(reserved_matches),
        "reserved_labels": reserved_matches,
        "status": status,
    }
