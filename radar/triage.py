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


# A full pull-request URL in a comment, possibly in another repo.
COMMENT_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
# A bare "#N" mention in a comment (e.g. "opened PR #1259", "see #1259").
# Ambiguous on its own - the caller must fetch #N to confirm it's actually a
# PR (not a plain issue) and get its state; see extract_comment_pr_mentions.
COMMENT_HASH_MENTION_RE = re.compile(r"#(\d+)\b")


def extract_comment_pr_mentions(
    comments: list[dict], issue_repo: str, issue_number: int
) -> list[tuple[str, int]]:
    """Candidate (repo, number) pairs mentioned in comments that MIGHT be
    PRs - GitHub's own cross-reference system only records a backlink on
    the MENTIONED item's timeline, not on the mentioning issue's own
    timeline, so a comment like "I opened PR #1259" or "see #1259" is
    otherwise invisible to analyze_linked_prs. Deduplicated, in order,
    excluding a mention of the issue itself."""
    seen: set[tuple[str, int]] = set()
    candidates: list[tuple[str, int]] = []

    def _add(repo: str, number: int) -> None:
        key = (repo.lower(), number)
        if key == (issue_repo.lower(), issue_number) or key in seen:
            return
        seen.add(key)
        candidates.append((repo, number))

    for comment in comments:
        body = comment.get("body") or ""
        for repo, number in COMMENT_PR_URL_RE.findall(body):
            _add(repo, int(number))
        for number in COMMENT_HASH_MENTION_RE.findall(body):
            _add(issue_repo, int(number))

    return candidates


@dataclass
class PRReference:
    number: int | None
    url: str
    repo: str
    author: str | None
    merged: bool
    open: bool
    same_repo: bool
    closes_this_issue: bool
    # "timeline" (a cross-referenced event) or "comment" (a comment mention
    # we resolved ourselves). A comment mention is direct human-stated
    # evidence, so it counts as HAS-PR without needing a closing keyword.
    source: str = "timeline"


@dataclass
class LinkedPRResult:
    has_linked_pr: bool
    unsure: bool
    likely_already_fixed: bool = False
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
        # GreedyBear's required PR title format is "<feature>. Closes #999" -
        # the keyword often lives in the title, not the body, so check both.
        closing_text = f"{src_issue.get('title') or ''}\n{src_issue.get('body') or ''}"
        refs.append(
            PRReference(
                number=src_issue.get("number"),
                url=src_issue.get("html_url") or pr_info.get("html_url") or "",
                repo=repo_full_name or "unknown",
                author=(src_issue.get("user") or {}).get("login"),
                merged=bool(pr_info.get("merged_at")),
                open=src_issue.get("state") == "open",
                same_repo=repo_full_name.lower() == issue_repo.lower(),
                closes_this_issue=issue_number in _closing_keyword_targets(closing_text),
                source="comment" if event.get("_mention_source") == "comment" else "timeline",
            )
        )
    return refs


def analyze_linked_prs(
    timeline: list[dict], issue_repo: str, issue_number: int, issue_author: str | None = None
) -> LinkedPRResult:
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
            elif ref.source == "comment":
                # A comment explicitly naming this PR is direct human-stated
                # evidence - no closing keyword needed to trust it.
                has_pr = True
                notes.append(f"PR #{ref.number} ({state_word}) mentioned in a comment")
            elif ref.open and issue_author and ref.author and ref.author == issue_author:
                # The issue-and-PR-pair pattern: same person opened both, no
                # closing keyword needed to trust this is the fix in progress.
                has_pr = True
                notes.append(f"issue author opened PR #{ref.number}")
            else:
                unsure = True
                notes.append(f"PR #{ref.number} ({state_word}) mentions this issue but has no closing keyword")
            continue

        # Same repo, neither open nor merged: closed without merging.
        notes.append(f"previous attempt #{ref.number} closed unmerged")

    likely_already_fixed = any(ref.same_repo and ref.merged for ref in refs)
    return LinkedPRResult(
        has_linked_pr=has_pr,
        unsure=unsure and not has_pr,
        likely_already_fixed=likely_already_fixed,
        notes=notes,
    )


def maintainer_replied(comments: list[dict], reviewers: list[str]) -> bool:
    """Whether any comment author is one of the repo's configured reviewers."""
    reviewer_logins = {r.lower() for r in reviewers}
    for comment in comments:
        login = (comment.get("user") or {}).get("login")
        if login and login.lower() in reviewer_logins:
            return True
    return False


def count_claim_comments(comments: list[dict], claim_phrases: list[str], exclude_login: str | None = None) -> int:
    """Count comments that look like claim attempts, excluding the issue's
    own author (their claims are the author-self-claim signal, not
    contention between different people - see author_self_claim)."""
    count = 0
    for comment in comments:
        if exclude_login and (comment.get("user") or {}).get("login") == exclude_login:
            continue
        body = (comment.get("body") or "").lower()
        if any(phrase in body for phrase in claim_phrases):
            count += 1
    return count


def _label_names(labels: list) -> list[str]:
    return [(label.get("name") if isinstance(label, dict) else str(label)).lower() for label in labels]


def matched_reserved_labels(labels: list, reserved_labels: list[str]) -> list[str]:
    names = _label_names(labels)
    return [name for name in names if name in reserved_labels]


def reserved_hints(issue: dict, reserved_labels: list[str]) -> list[str]:
    """Reasons to treat this issue as reserved/DISCUSS-ONLY: matched labels
    plus a "GSoC" mention in the title, which gets the same treatment as a
    reserved label even without one actually being applied."""
    hints = matched_reserved_labels(issue.get("labels", []), reserved_labels)
    if "gsoc" in (issue.get("title") or "").lower():
        hints.append("title mentions GSoC")
    return hints


# Title prefixes that, on their own, are a strong hint the reporter intends
# to do the work themselves (common in these repos' GSoC-adjacent issues).
AUTHOR_CLAIM_TITLE_PREFIXES = ("proposal:",)


def author_self_claim(issue: dict, comments: list[dict], claim_phrases: list[str]) -> str | None:
    """None, or a human-readable reason the issue's own body, the author's
    own follow-up comments, or its title suggest its author intends to
    submit the fix themselves. A claim phrase in a comment only counts when
    the commenter IS the issue's own author - other people's comments are
    handled separately (see count_claim_comments/CONTESTED)."""
    body = (issue.get("body") or "").lower()
    matched_phrase = next((p for p in claim_phrases if p in body), None)
    if matched_phrase:
        return f'issue body says: "{matched_phrase}"'

    issue_author = (issue.get("user") or {}).get("login")
    if issue_author:
        for comment in comments:
            if (comment.get("user") or {}).get("login") != issue_author:
                continue
            comment_body = (comment.get("body") or "").lower()
            matched_phrase = next((p for p in claim_phrases if p in comment_body), None)
            if matched_phrase:
                return f'author comment says: "{matched_phrase}"'

    title = (issue.get("title") or "").strip().lower()
    if title.startswith(AUTHOR_CLAIM_TITLE_PREFIXES):
        return 'title starts with "Proposal:"'

    return None


DOCS_LABEL_NAMES = {"docs", "documentation"}
BODY_EXCERPT_CHARS = 500


def _is_docs_issue(issue: dict) -> bool:
    if DOCS_LABEL_NAMES & set(_label_names(issue.get("labels", []))):
        return True
    title = (issue.get("title") or "").strip().lower()
    return title.startswith("docs") or title.startswith("documentation")


def compute_fit(issue: dict, positive_keywords: list[str], negative_keywords: list[str]) -> tuple[str, list[str]]:
    if _is_docs_issue(issue):
        return "DOCS", ["docs"]

    # Labels and title are a strong, deliberate signal; the body is noisy
    # (e.g. a long dependency list can contain negative keywords that have
    # nothing to do with what the issue is actually about), so it only
    # gets searched in its first ~500 chars and can contribute at most one
    # point each way, regardless of how many keywords it happens to match.
    strong_text = " ".join(_label_names(issue.get("labels", [])) + [(issue.get("title") or "").lower()])
    weak_text = (issue.get("body") or "")[:BODY_EXCERPT_CHARS].lower()

    strong_positive = [k for k in positive_keywords if k in strong_text]
    strong_negative = [k for k in negative_keywords if k in strong_text]
    weak_positive = [k for k in positive_keywords if k not in strong_positive and k in weak_text]
    weak_negative = [k for k in negative_keywords if k not in strong_negative and k in weak_text]

    matched_positive = strong_positive + weak_positive
    matched_negative = strong_negative + weak_negative

    score = 2 * len(strong_positive) - 2 * len(strong_negative)
    score += 1 if weak_positive else 0
    score -= 1 if weak_negative else 0

    if not matched_negative:
        return ("GOOD", matched_positive) if matched_positive else ("MAYBE", [])
    if not matched_positive:
        return "SKIP", matched_negative

    combined = matched_positive + [f"-{k}" for k in matched_negative]
    if score > 0:
        return "GOOD", combined
    if score < 0:
        return "SKIP", combined
    return "MAYBE", combined


def days_since(timestamp: str | None, now: datetime | None = None) -> int | None:
    if not timestamp:
        return None
    now = now or datetime.now(timezone.utc)
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (now - dt).days


def compute_status(
    assigned: bool,
    linked: LinkedPRResult,
    contested: bool,
    reserved: bool,
    author_claimed: bool = False,
    comment_claimed: bool = False,
) -> str:
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
    if comment_claimed:
        return "COMMENT-CLAIMED"
    if author_claimed:
        return "AUTHOR-CLAIMED"
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
    issue_author = (issue.get("user") or {}).get("login")
    linked = analyze_linked_prs(timeline, repo_name, issue["number"], issue_author=issue_author)
    claim_count = count_claim_comments(comments, claim_phrases, exclude_login=issue_author)
    contested = claim_count >= 2 and not assigned
    comment_claimed = claim_count == 1 and not assigned
    fit_tag, matched_keywords = compute_fit(issue, positive_keywords, negative_keywords)
    reserved_matches = reserved_hints(issue, reserved_labels)
    author_claim_reason = author_self_claim(issue, comments, claim_phrases)
    status = compute_status(
        assigned, linked, contested, bool(reserved_matches), bool(author_claim_reason), comment_claimed
    )

    return {
        "number": issue["number"],
        "title": issue.get("title"),
        "assigned": assigned,
        "has_linked_pr": linked.has_linked_pr,
        "linked_pr_unsure": linked.unsure,
        "likely_already_fixed": linked.likely_already_fixed,
        "linked_pr_notes": linked.notes,
        "claim_comments": claim_count,
        "contested": contested,
        "comment_claimed": comment_claimed,
        "fit_tag": fit_tag,
        "matched_keywords": matched_keywords,
        "staleness_days": days_since(issue.get("updated_at"), now=now),
        "reserved": bool(reserved_matches),
        "reserved_hints": reserved_matches,
        "author_claim_reason": author_claim_reason,
        "status": status,
    }
