from datetime import datetime, timezone

from radar.triage import (
    LinkedPRResult,
    analyze_linked_prs,
    compute_fit,
    compute_status,
    count_claim_comments,
    days_since,
    matched_reserved_labels,
    triage_issue,
)

REPO = "owner/repo"


def cross_ref(
    number,
    body="",
    state="open",
    merged_at=None,
    repo_full_name=REPO,
    is_pr=True,
):
    source_issue = {
        "number": number,
        "state": state,
        "html_url": f"https://github.com/{repo_full_name}/pull/{number}",
        "body": body,
        "repository": {"full_name": repo_full_name},
    }
    if is_pr:
        source_issue["pull_request"] = {
            "url": f"https://api.github.com/repos/{repo_full_name}/pulls/{number}",
            "html_url": f"https://github.com/{repo_full_name}/pull/{number}",
            "merged_at": merged_at,
        }
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": source_issue}}


class TestAnalyzeLinkedPRs:
    def test_merged_pr_with_closing_keyword_is_has_pr(self):
        # Merged into a non-default branch still reports merged_at; GitHub
        # won't auto-close the issue itself, but the timeline event is the
        # same either way.
        timeline = [cross_ref(7, body="Fixes #42", merged_at="2026-02-01T00:00:00Z", state="closed")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True
        assert result.unsure is False
        assert "PR #7 (merged)" in result.notes[0]

    def test_open_pr_with_closing_keyword_is_has_pr(self):
        timeline = [cross_ref(7, body="Closes #42", state="open")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True
        assert result.unsure is False

    def test_closed_unmerged_pr_does_not_block(self):
        timeline = [cross_ref(9, body="Fixes #42", state="closed", merged_at=None)]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is False
        assert "previous attempt #9 closed unmerged" in result.notes

    def test_mention_without_closing_keyword_is_unsure_not_has_pr(self):
        timeline = [cross_ref(7, body="See also #42 for context", state="open")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is True
        assert "no closing keyword" in result.notes[0]

    def test_plain_issue_cross_reference_is_ignored(self):
        timeline = [cross_ref(7, body="Fixes #42", is_pr=False)]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is False
        assert result.notes == []

    def test_fork_pr_is_unsure_not_has_pr(self):
        timeline = [
            cross_ref(7, body="Fixes #42", state="open", repo_full_name="someone-else/repo-fork")
        ]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is True
        assert "possible work in progress elsewhere" in result.notes[0]
        assert "someone-else/repo-fork" in result.notes[0]

    def test_fork_pr_even_when_merged_is_still_unsure(self):
        timeline = [
            cross_ref(
                7, body="Fixes #42", merged_at="2026-01-01T00:00:00Z", repo_full_name="a-fork/repo"
            )
        ]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is True

    def test_one_closing_merged_pr_wins_over_an_unrelated_closed_unmerged_one(self):
        timeline = [
            cross_ref(9, body="Fixes #42", state="closed", merged_at=None),
            cross_ref(12, body="Closes #42", state="open"),
        ]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True
        assert result.unsure is False
        assert any("previous attempt #9" in n for n in result.notes)

    def test_has_pr_takes_priority_over_ambiguous_reference(self):
        timeline = [
            cross_ref(12, body="Closes #42", state="open"),
            cross_ref(13, body="related to #42", state="open"),
        ]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True
        assert result.unsure is False

    def test_no_references_at_all(self):
        result = analyze_linked_prs([], REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is False
        assert result.notes == []

    def test_other_timeline_event_types_are_ignored(self):
        timeline = [{"event": "labeled", "label": {"name": "bug"}}]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.notes == []


class TestClaimComments:
    def test_counts_matching_phrases(self):
        comments = [
            {"body": "I'd like to work on this"},
            {"body": "unrelated comment"},
            {"body": "Can I work on this one?"},
        ]
        assert count_claim_comments(comments, ["i'd like to work", "can i work"]) == 2

    def test_no_comments(self):
        assert count_claim_comments([], ["assign me"]) == 0


class TestFit:
    def test_good_when_only_positive_matches(self):
        issue = {"title": "Fix django security bug", "body": "", "labels": []}
        fit, matched = compute_fit(issue, ["django", "security"], ["react"])
        assert fit == "GOOD"
        assert set(matched) == {"django", "security"}

    def test_skip_when_only_negative_matches(self):
        issue = {"title": "Update React frontend styling", "body": "", "labels": []}
        fit, matched = compute_fit(issue, ["python"], ["react", "css"])
        assert fit == "SKIP"
        assert set(matched) == {"react"}

    def test_maybe_when_both_match(self):
        issue = {"title": "Python API with React UI", "body": "", "labels": []}
        fit, matched = compute_fit(issue, ["python", "api"], ["react", "ui"])
        assert fit == "MAYBE"

    def test_maybe_when_neither_matches(self):
        issue = {"title": "Improve docs", "body": "", "labels": []}
        fit, matched = compute_fit(issue, ["python"], ["react"])
        assert fit == "MAYBE"
        assert matched == []

    def test_label_names_are_searched(self):
        issue = {"title": "", "body": "", "labels": [{"name": "security"}]}
        fit, matched = compute_fit(issue, ["security"], [])
        assert fit == "GOOD"


class TestReservedLabels:
    def test_matched_reserved_label(self):
        labels = [{"name": "gsoc-idea"}, {"name": "bug"}]
        assert matched_reserved_labels(labels, ["gsoc-idea", "do-not-work-on"]) == ["gsoc-idea"]

    def test_no_match(self):
        labels = [{"name": "bug"}]
        assert matched_reserved_labels(labels, ["gsoc-idea"]) == []


class TestDaysSince:
    def test_computes_day_difference(self):
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert days_since("2026-02-01T00:00:00Z", now=now) == 28

    def test_none_when_missing(self):
        assert days_since(None) is None


class TestComputeStatus:
    def test_unsure_overrides_everything(self):
        linked = LinkedPRResult(has_linked_pr=True, unsure=True)
        assert compute_status(assigned=True, linked=linked, contested=True, reserved=True) == "UNSURE"

    def test_has_pr_before_discuss_only(self):
        linked = LinkedPRResult(has_linked_pr=True, unsure=False)
        assert compute_status(assigned=False, linked=linked, contested=False, reserved=True) == "HAS-PR"

    def test_reserved_before_contested(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        assert compute_status(assigned=False, linked=linked, contested=True, reserved=True) == "DISCUSS-ONLY"

    def test_contested_before_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        assert compute_status(assigned=True, linked=linked, contested=True, reserved=False) == "CONTESTED"

    def test_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        assert compute_status(assigned=True, linked=linked, contested=False, reserved=False) == "CLAIMED"

    def test_open_free(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        assert compute_status(assigned=False, linked=linked, contested=False, reserved=False) == "OPEN-FREE"


class TestTriageIssue:
    def test_full_open_free_issue(self):
        issue = {
            "number": 42,
            "title": "Add a Python scanner module",
            "body": "security bug in the scanner",
            "labels": [{"name": "bug"}],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        result = triage_issue(
            issue=issue,
            comments=[],
            timeline=[],
            repo_name=REPO,
            positive_keywords=["python", "security", "scanner"],
            negative_keywords=["react"],
            claim_phrases=["assign me"],
            reserved_labels=["gsoc-idea"],
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        assert result["status"] == "OPEN-FREE"
        assert result["fit_tag"] == "GOOD"
        assert result["staleness_days"] == 31
        assert result["has_linked_pr"] is False

    def test_assigned_issue_is_claimed(self):
        issue = {
            "number": 1,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [{"login": "someone"}],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        result = triage_issue(
            issue, [], [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=[],
        )
        assert result["status"] == "CLAIMED"
        assert result["assigned"] is True

    def test_contested_issue_with_two_claims_and_no_assignee(self):
        issue = {
            "number": 2,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        comments = [{"body": "I'll take this"}, {"body": "assign me please"}]
        result = triage_issue(
            issue, comments, [], REPO,
            positive_keywords=[], negative_keywords=[],
            claim_phrases=["i'll take", "assign me"], reserved_labels=[],
        )
        assert result["status"] == "CONTESTED"
        assert result["claim_comments"] == 2

    def test_reserved_issue_is_discuss_only(self):
        issue = {
            "number": 3,
            "title": "x",
            "body": "",
            "labels": [{"name": "gsoc-idea"}],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        result = triage_issue(
            issue, [], [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=["gsoc-idea"],
        )
        assert result["status"] == "DISCUSS-ONLY"
        assert result["reserved_labels"] == ["gsoc-idea"]

    def test_closed_unmerged_previous_attempt_stays_open_free(self):
        issue = {
            "number": 4,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        timeline = [cross_ref(99, body="Fixes #4", state="closed", merged_at=None)]
        result = triage_issue(
            issue, [], timeline, REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=[],
        )
        assert result["status"] == "OPEN-FREE"
        assert result["has_linked_pr"] is False
        assert "previous attempt #99 closed unmerged" in result["linked_pr_notes"]

    def test_merged_pr_into_non_default_branch_is_has_pr(self):
        issue = {
            "number": 5,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "state": "open",  # issue stays open: merge target wasn't the default branch
            "updated_at": "2026-01-01T00:00:00Z",
        }
        timeline = [cross_ref(50, body="Closes #5", state="closed", merged_at="2026-01-15T00:00:00Z")]
        result = triage_issue(
            issue, [], timeline, REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=[],
        )
        assert result["status"] == "HAS-PR"
        assert result["has_linked_pr"] is True
