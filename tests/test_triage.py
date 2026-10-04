from datetime import datetime, timezone
from pathlib import Path

from radar.config import load_config
from radar.triage import (
    LinkedPRResult,
    analyze_linked_prs,
    author_self_claim,
    compute_fit,
    compute_status,
    count_claim_comments,
    days_since,
    extract_comment_pr_mentions,
    maintainer_replied,
    matched_reserved_labels,
    reserved_hints,
    triage_issue,
)

REPO = "owner/repo"


def cross_ref(
    number,
    body="",
    title="",
    state="open",
    merged_at=None,
    repo_full_name=REPO,
    is_pr=True,
    author=None,
):
    source_issue = {
        "number": number,
        "state": state,
        "html_url": f"https://github.com/{repo_full_name}/pull/{number}",
        "title": title,
        "body": body,
        "repository": {"full_name": repo_full_name},
        "user": {"login": author} if author else None,
    }
    if is_pr:
        source_issue["pull_request"] = {
            "url": f"https://api.github.com/repos/{repo_full_name}/pulls/{number}",
            "html_url": f"https://github.com/{repo_full_name}/pull/{number}",
            "merged_at": merged_at,
        }
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": source_issue}}


def comment_mention_event(number, state="open", merged_at=None, repo_full_name=REPO, is_pr=True, author=None):
    """Synthetic timeline event shaped like what main.py builds after
    resolving a comment-mentioned PR number via GitHubClient.get_issue()."""
    event = cross_ref(
        number, state=state, merged_at=merged_at, repo_full_name=repo_full_name, is_pr=is_pr, author=author
    )
    event["_mention_source"] = "comment"
    return event


class TestExtractCommentPrMentions:
    def test_bare_hash_mention(self):
        comments = [{"body": "I opened PR #1259 for this."}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [(REPO, 1259)]

    def test_see_hash_mention(self):
        comments = [{"body": "see #1259"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [(REPO, 1259)]

    def test_full_pull_url_mention(self):
        comments = [{"body": "Fixed in https://github.com/other/repo/pull/99"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [("other/repo", 99)]

    def test_full_pull_url_in_same_repo(self):
        comments = [{"body": f"See https://github.com/{REPO}/pull/77"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [(REPO, 77)]

    def test_excludes_self_mention(self):
        comments = [{"body": "duplicate of #42"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == []

    def test_deduplicates_across_comments(self):
        comments = [{"body": "see #1259"}, {"body": "already mentioned #1259 above"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [(REPO, 1259)]

    def test_multiple_distinct_mentions_in_order(self):
        comments = [{"body": "see #10 and also #20"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == [(REPO, 10), (REPO, 20)]

    def test_no_mentions(self):
        comments = [{"body": "just a regular comment"}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == []

    def test_no_comments(self):
        assert extract_comment_pr_mentions([], REPO, 42) == []

    def test_missing_body_does_not_crash(self):
        comments = [{"body": None}, {}]
        assert extract_comment_pr_mentions(comments, REPO, 42) == []


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

    def test_closing_keyword_in_title_only_is_has_pr(self):
        # GreedyBear requires PR titles like "<feature>. Closes #999" - the
        # keyword lives in the title, not the body.
        timeline = [cross_ref(7, title="Add retry logic. Closes #42", body="", state="open")]
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

    def test_same_author_open_pr_without_closing_keyword_is_has_pr(self):
        # The issue-and-PR-pair pattern (IntelOwl #4047, Nettacker #1521
        # style): same person opened both, no closing keyword needed.
        timeline = [cross_ref(7, body="see #42 for context", state="open", author="dev-aditya")]
        result = analyze_linked_prs(timeline, REPO, 42, issue_author="dev-aditya")
        assert result.has_linked_pr is True
        assert result.unsure is False
        assert "issue author opened PR #7" in result.notes[0]

    def test_different_author_bare_mention_stays_unsure(self):
        timeline = [cross_ref(7, body="see #42 for context", state="open", author="someone-else")]
        result = analyze_linked_prs(timeline, REPO, 42, issue_author="dev-aditya")
        assert result.has_linked_pr is False
        assert result.unsure is True

    def test_same_author_heuristic_does_not_apply_when_merged_not_open(self):
        # Spec says "same author AND open" - a merged PR by the same
        # author without a closing keyword stays an ambiguous mention,
        # not an automatic HAS-PR.
        timeline = [
            cross_ref(7, body="see #42", state="closed", merged_at="2026-01-01T00:00:00Z", author="dev-aditya")
        ]
        result = analyze_linked_prs(timeline, REPO, 42, issue_author="dev-aditya")
        assert result.has_linked_pr is False
        assert result.unsure is True

    def test_closing_keyword_still_wins_over_author_match_path(self):
        timeline = [cross_ref(7, body="Closes #42", state="open", author="dev-aditya")]
        result = analyze_linked_prs(timeline, REPO, 42, issue_author="dev-aditya")
        assert result.has_linked_pr is True
        assert "references closing this issue" in result.notes[0]

    def test_no_issue_author_provided_falls_back_to_ambiguous(self):
        timeline = [cross_ref(7, body="see #42", state="open", author="dev-aditya")]
        result = analyze_linked_prs(timeline, REPO, 42, issue_author=None)
        assert result.has_linked_pr is False
        assert result.unsure is True


class TestCommentMentionedPRs:
    def test_comment_mentioned_open_pr_is_has_pr_without_closing_keyword(self):
        # No closing keyword in body/title at all - just the fact that it
        # came from a resolved comment mention is enough.
        timeline = [comment_mention_event(1259, state="open")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True
        assert result.unsure is False
        assert "mentioned in a comment" in result.notes[0]

    def test_comment_mentioned_merged_pr_is_has_pr(self):
        timeline = [comment_mention_event(1259, state="closed", merged_at="2026-01-01T00:00:00Z")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True

    def test_comment_mentioned_closed_unmerged_pr_is_a_note_only(self):
        timeline = [comment_mention_event(1259, state="closed", merged_at=None)]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is False
        assert "previous attempt #1259 closed unmerged" in result.notes

    def test_comment_mentioned_pr_in_another_repo_is_unsure_not_has_pr(self):
        timeline = [comment_mention_event(99, state="open", repo_full_name="other/repo")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is False
        assert result.unsure is True
        assert "possible work in progress elsewhere" in result.notes[0]

    def test_closing_keyword_from_timeline_and_comment_mention_both_count(self):
        timeline = [
            comment_mention_event(1259, state="open"),
            cross_ref(99, body="Fixes #42", state="closed", merged_at=None),
        ]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.has_linked_pr is True


class TestLikelyAlreadyFixed:
    def test_true_when_same_repo_merged_pr_exists_even_if_unsure(self):
        timeline = [cross_ref(7, body="see #42", merged_at="2026-01-01T00:00:00Z", state="closed")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.unsure is True  # ambiguous mention, no closing keyword
        assert result.likely_already_fixed is True

    def test_false_when_only_closed_unmerged(self):
        timeline = [cross_ref(7, body="Fixes #42", state="closed", merged_at=None)]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.likely_already_fixed is False

    def test_false_for_fork_merge(self):
        timeline = [cross_ref(7, body="see #42", merged_at="2026-01-01T00:00:00Z", repo_full_name="a-fork/repo")]
        result = analyze_linked_prs(timeline, REPO, 42)
        assert result.likely_already_fixed is False

    def test_false_when_no_references(self):
        result = analyze_linked_prs([], REPO, 42)
        assert result.likely_already_fixed is False


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

    def test_excludes_the_issue_authors_own_comments(self):
        # The #1747 scenario: the SAME person makes two claim-like
        # comments about their own issue. That's an author self-claim,
        # not contention between different people, so it shouldn't count
        # toward CONTESTED at all.
        comments = [
            {"user": {"login": "reporter42"}, "body": "Could you please assign this issue to me?"},
            {"user": {"login": "reporter42"}, "body": "I have a version ready whenever you want it."},
        ]
        count = count_claim_comments(
            comments, ["could you assign", "i have a version ready"], exclude_login="reporter42"
        )
        assert count == 0

    def test_other_peoples_claims_still_counted_when_excluding_author(self):
        comments = [
            {"user": {"login": "reporter42"}, "body": "could you assign this to me"},
            {"user": {"login": "someone-else"}, "body": "i'll take"},
            {"user": {"login": "a-third-person"}, "body": "i can work on this"},
        ]
        count = count_claim_comments(
            comments, ["could you assign", "i'll take", "i can work on this"], exclude_login="reporter42"
        )
        assert count == 2


class TestMaintainerReplied:
    def test_true_when_reviewer_commented(self):
        comments = [{"user": {"login": "someuser"}}, {"user": {"login": "securestep9"}}]
        assert maintainer_replied(comments, ["securestep9", "arkid15r"]) is True

    def test_false_when_no_reviewer_commented(self):
        comments = [{"user": {"login": "someuser"}}, {"user": {"login": "another"}}]
        assert maintainer_replied(comments, ["securestep9"]) is False

    def test_false_with_no_comments(self):
        assert maintainer_replied([], ["securestep9"]) is False

    def test_case_insensitive_login_match(self):
        comments = [{"user": {"login": "SecureStep9"}}]
        assert maintainer_replied(comments, ["securestep9"]) is True

    def test_comment_with_missing_user_is_ignored(self):
        comments = [{"user": None}, {"body": "no user key at all"}]
        assert maintainer_replied(comments, ["securestep9"]) is False


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


class TestDocsDetector:
    def test_title_prefix_docs_colon(self):
        # Nettacker #1758 style.
        issue = {
            "title": "docs: fix outdated tcp_connect_port_scan references in Usage.md",
            "body": "",
            "labels": [],
        }
        fit, matched = compute_fit(issue, ["module", "yaml"], ["react"])
        assert fit == "DOCS"
        assert matched == ["docs"]

    def test_title_prefix_documentation(self):
        issue = {"title": "Documentation: update install guide", "body": "", "labels": []}
        fit, _ = compute_fit(issue, [], [])
        assert fit == "DOCS"

    def test_docs_label(self):
        issue = {"title": "Fix a typo", "body": "", "labels": [{"name": "documentation"}]}
        fit, _ = compute_fit(issue, [], [])
        assert fit == "DOCS"

    def test_docs_word_mid_title_does_not_trigger(self):
        # Only a prefix match counts, not "docs" appearing anywhere.
        issue = {"title": "Improve docs", "body": "", "labels": []}
        fit, _ = compute_fit(issue, ["improve"], [])
        assert fit != "DOCS"

    def test_docs_overrides_keyword_scoring_entirely(self):
        issue = {
            "title": "docs: security scanner module cleanup",
            "body": "react frontend css ui javascript-only",
            "labels": [{"name": "bug"}],
        }
        fit, matched = compute_fit(issue, ["security", "scanner", "module", "bug"], ["react", "frontend"])
        assert fit == "DOCS"
        assert matched == ["docs"]


class TestFitWeighting:
    def test_label_signal_dominates_noisy_dependency_list_body(self):
        # IntelOwl #3973 style: "bug" label plus a title with no keyword
        # signal, and a body that's a long dependency/vuln report
        # mentioning several negative keywords. Old behavior: body
        # negatives + no title positive -> MAYBE. New behavior: the
        # strong "bug" label signal should dominate -> GOOD.
        body = (
            "## What happened\nI am not sure if we are concerned or this is a known thing.\n"
            "trivy vuln scanner flagged:\n"
            + "\n".join(f"- react-{i}, frontend-lib-{i}, css-loader-{i}, ui-kit-{i}" for i in range(20))
        )
        issue = {
            "title": "Deprecated and vulnerable dependencies",
            "body": body,
            "labels": [{"name": "bug"}, {"name": "stale"}],
        }
        positive = ["python", "django", "bug", "security", "scanner", "module", "yaml", "test", "api"]
        negative = ["react", "frontend", "css", "ui", "javascript-only", "translation"]

        fit, matched = compute_fit(issue, positive, negative)

        assert fit == "GOOD"
        assert "bug" in matched

    def test_negative_keywords_past_500_chars_are_ignored(self):
        padding = "x" * 600
        issue = {
            "title": "A backend python tool",
            "body": padding + " react frontend",  # negative keywords land past the cutoff
            "labels": [],
        }
        fit, matched = compute_fit(issue, ["python"], ["react", "frontend"])
        assert fit == "GOOD"
        assert not any(k in matched for k in ["react", "frontend", "-react", "-frontend"])

    def test_negative_keywords_within_500_chars_still_count(self):
        issue = {
            "title": "A backend tool",  # no strong signal either way
            "body": "react frontend",  # within the first 500 chars
            "labels": [],
        }
        fit, matched = compute_fit(issue, [], ["react", "frontend"])
        assert fit == "SKIP"

    def test_body_contribution_is_capped_regardless_of_match_count(self):
        # No strong (label/title) signal; body has many positive AND many
        # negative keyword hits within the first 500 chars. Each side
        # should contribute at most one point, netting to a tie -> MAYBE,
        # not a SKIP or GOOD driven by raw match count.
        issue = {
            "title": "",
            "body": "python django security scanner module react frontend css ui",
            "labels": [],
        }
        positive = ["python", "django", "security", "scanner", "module"]
        negative = ["react", "frontend", "css", "ui"]
        fit, matched = compute_fit(issue, positive, negative)
        assert fit == "MAYBE"
        assert set(matched) == {
            "python", "django", "security", "scanner", "module",
            "-react", "-frontend", "-css", "-ui",
        }

    def test_strong_positive_in_title_beats_body_negative_noise(self):
        issue = {
            "title": "python security module fix",
            "body": "mentions react and frontend in passing",
            "labels": [],
        }
        fit, _ = compute_fit(issue, ["python", "security", "module"], ["react", "frontend"])
        assert fit == "GOOD"


class TestReservedLabels:
    def test_matched_reserved_label(self):
        labels = [{"name": "gsoc-idea"}, {"name": "bug"}]
        assert matched_reserved_labels(labels, ["gsoc-idea", "do-not-work-on"]) == ["gsoc-idea"]

    def test_no_match(self):
        labels = [{"name": "bug"}]
        assert matched_reserved_labels(labels, ["gsoc-idea"]) == []


class TestReservedHints:
    def test_label_hint(self):
        issue = {"title": "x", "labels": [{"name": "gsoc-idea"}]}
        assert reserved_hints(issue, ["gsoc-idea"]) == ["gsoc-idea"]

    def test_title_gsoc_hint(self):
        issue = {"title": "Proposal: Add module - GSoC 2026", "labels": []}
        assert reserved_hints(issue, []) == ["title mentions GSoC"]

    def test_title_gsoc_case_insensitive(self):
        issue = {"title": "gsoc idea for next summer", "labels": []}
        assert reserved_hints(issue, []) == ["title mentions GSoC"]

    def test_both_label_and_title_hint(self):
        issue = {"title": "GSoC project idea", "labels": [{"name": "gsoc-idea"}]}
        assert reserved_hints(issue, ["gsoc-idea"]) == ["gsoc-idea", "title mentions GSoC"]

    def test_no_hints(self):
        issue = {"title": "Fix a bug", "labels": [{"name": "bug"}]}
        assert reserved_hints(issue, ["gsoc-idea"]) == []


class TestAuthorSelfClaim:
    def test_body_phrase_match(self):
        issue = {"title": "Add a feature", "body": "I'd like to work on this myself."}
        reason = author_self_claim(issue, [], ["i'd like to work"])
        assert reason is not None
        assert "i'd like to work" in reason

    def test_happy_to_submit_a_pr_phrase(self):
        issue = {"title": "Add a feature", "body": "Happy to submit a PR for this."}
        reason = author_self_claim(issue, [], ["happy to submit a pr"])
        assert reason is not None

    def test_i_can_implement_this_phrase(self):
        issue = {"title": "Add a feature", "body": "I can implement this if no one else is on it."}
        reason = author_self_claim(issue, [], ["i can implement this"])
        assert reason is not None

    def test_greedybear_1668_exact_sentence(self):
        # Real example that was missed before "happy to take" was added:
        # GreedyBear #1668's body contains this sentence verbatim.
        issue = {
            "title": "Over long hostname in an attacker URL drops a whole honeypot's IOCs",
            "body": "Happy to take it if the shape looks right.",
        }
        reason = author_self_claim(issue, [], ["happy to take"])
        assert reason is not None
        assert "happy to take" in reason

    def test_additional_claim_phrase_variants(self):
        cases = [
            ("I can take this one on.", "i can take"),
            ("Happy to work on this over the weekend.", "happy to work on"),
            ("I'll take this and send a PR soon.", "i'll take this"),
            ("I can work on the fix now.", "i can work on"),
        ]
        for body, phrase in cases:
            issue = {"title": "x", "body": body}
            assert author_self_claim(issue, [], [phrase]) is not None, body

    def test_proposal_title_prefix_alone_is_sufficient(self):
        issue = {"title": "Proposal: Add KEV module for CVE-2026-1234", "body": "Some description."}
        reason = author_self_claim(issue, [], [])
        assert reason is not None
        assert "Proposal" in reason

    def test_proposal_prefix_is_case_insensitive(self):
        issue = {"title": "PROPOSAL: do a thing", "body": ""}
        assert author_self_claim(issue, [], []) is not None

    def test_no_claim_signal_returns_none(self):
        issue = {"title": "A plain bug report", "body": "It crashes when I run it."}
        assert author_self_claim(issue, [], ["i'd like to work"]) is None

    def test_greedybear_1668_matches_against_real_config_claim_phrases(self):
        # End-to-end: the actual config.yaml claim_phrases list, not a
        # hand-picked single phrase, must catch this real example.
        config_path = Path(__file__).parent.parent / "radar" / "config.yaml"
        config = load_config(config_path)
        issue = {
            "title": "Over long hostname in an attacker URL drops a whole honeypot's IOCs",
            "body": "Happy to take it if the shape looks right.",
        }
        assert author_self_claim(issue, [], config.claim_phrases) is not None

    def test_proposal_word_mid_title_does_not_count(self):
        issue = {"title": "Our proposal process needs docs", "body": ""}
        assert author_self_claim(issue, [], []) is None

    def test_nettacker_1758_exact_sentence(self):
        issue = {
            "title": "docs: fix outdated tcp_connect_port_scan references in Usage.md",
            "body": "If this looks good, I can make the documentation change and submit a PR.",
        }
        reason = author_self_claim(issue, [], ["i can make", "submit a pr"])
        assert reason is not None

    def test_nettacker_1758_matches_against_real_config_claim_phrases(self):
        config = load_config(Path(__file__).parent.parent / "radar" / "config.yaml")
        issue = {
            "title": "docs: fix outdated tcp_connect_port_scan references in Usage.md",
            "body": "If this looks good, I can make the documentation change and submit a PR.",
        }
        assert author_self_claim(issue, [], config.claim_phrases) is not None

    def test_nettacker_1747_body_exact_sentence(self):
        issue = {
            "title": "x",
            "body": "I would like to be assigned to this issue to prepare a clean PR",
        }
        reason = author_self_claim(issue, [], ["i would like to be assigned"])
        assert reason is not None
        assert "body" in reason

    def test_nettacker_1747_author_comments_exact_sentences(self):
        # Body alone doesn't match here - only the author's own follow-up
        # comments do. Both comments are from the issue's own author.
        issue = {"title": "x", "body": "unrelated", "user": {"login": "reporter42"}}
        comments = [
            {"user": {"login": "reporter42"}, "body": "Could you please assign this issue to me?"},
            {"user": {"login": "reporter42"}, "body": "I have a version ready whenever you want it."},
        ]
        reason = author_self_claim(issue, comments, ["could you assign", "i have a version ready"])
        assert reason is not None
        assert "comment" in reason

    def test_nettacker_1747_matches_against_real_config_claim_phrases(self):
        config = load_config(Path(__file__).parent.parent / "radar" / "config.yaml")
        issue = {
            "title": "x",
            "body": "I would like to be assigned to this issue to prepare a clean PR",
            "user": {"login": "reporter42"},
        }
        comments = [
            {"user": {"login": "reporter42"}, "body": "Could you please assign this issue to me?"},
            {"user": {"login": "reporter42"}, "body": "I have a version ready whenever you want it."},
        ]
        assert author_self_claim(issue, comments, config.claim_phrases) is not None

    def test_comment_from_someone_other_than_the_author_does_not_count(self):
        issue = {"title": "x", "body": "", "user": {"login": "reporter42"}}
        comments = [{"user": {"login": "someone-else"}, "body": "I have a version ready to send over"}]
        assert author_self_claim(issue, comments, ["i have a version ready"]) is None

    def test_comment_claim_with_no_issue_author_set_does_not_crash(self):
        issue = {"title": "x", "body": "", "user": None}
        comments = [{"user": {"login": "someone"}, "body": "i can make this change"}]
        assert author_self_claim(issue, comments, ["i can make"]) is None


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

    def test_author_claimed_when_nothing_else_applies(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False, linked=linked, contested=False, reserved=False, author_claimed=True
        )
        assert status == "AUTHOR-CLAIMED"

    def test_claimed_takes_priority_over_author_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=True, linked=linked, contested=False, reserved=False, author_claimed=True
        )
        assert status == "CLAIMED"

    def test_contested_takes_priority_over_author_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False, linked=linked, contested=True, reserved=False, author_claimed=True
        )
        assert status == "CONTESTED"

    def test_reserved_takes_priority_over_author_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False, linked=linked, contested=False, reserved=True, author_claimed=True
        )
        assert status == "DISCUSS-ONLY"

    def test_comment_claimed_when_nothing_else_applies(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False, linked=linked, contested=False, reserved=False, comment_claimed=True
        )
        assert status == "COMMENT-CLAIMED"

    def test_comment_claimed_takes_priority_over_author_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False,
            linked=linked,
            contested=False,
            reserved=False,
            author_claimed=True,
            comment_claimed=True,
        )
        assert status == "COMMENT-CLAIMED"

    def test_claimed_takes_priority_over_comment_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=True, linked=linked, contested=False, reserved=False, comment_claimed=True
        )
        assert status == "CLAIMED"

    def test_contested_takes_priority_over_comment_claimed(self):
        linked = LinkedPRResult(has_linked_pr=False, unsure=False)
        status = compute_status(
            assigned=False, linked=linked, contested=True, reserved=False, comment_claimed=True
        )
        assert status == "CONTESTED"


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
        assert result["reserved_hints"] == ["gsoc-idea"]

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

    def test_proposal_issue_with_no_assignee_is_author_claimed(self):
        issue = {
            "number": 6,
            "title": "Proposal: Add module for CVE-2026-1234",
            "body": "",
            "labels": [],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        result = triage_issue(
            issue, [], [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=[],
        )
        assert result["status"] == "AUTHOR-CLAIMED"
        assert "Proposal" in result["author_claim_reason"]

    def test_gsoc_in_title_is_discuss_only_even_without_label(self):
        issue = {
            "number": 7,
            "title": "Proposal: new scanner module - GSoC 2026",
            "body": "",
            "labels": [],
            "assignees": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }
        result = triage_issue(
            issue, [], [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=["gsoc-idea"],
        )
        # Both a Proposal-title author-claim hint AND a GSoC-title reserved
        # hint apply here; reserved/DISCUSS-ONLY must win.
        assert result["status"] == "DISCUSS-ONLY"
        assert result["reserved_hints"] == ["title mentions GSoC"]

    def test_nettacker_1747_full_scenario_is_author_claimed_not_contested(self):
        # Real Nettacker #1747 shape: body claims a desire to be assigned,
        # and the SAME author posts two follow-up claim-like comments.
        # Before excluding the author from count_claim_comments, this
        # would have wrongly hit CONTESTED (2 "claim" comments, unassigned)
        # instead of AUTHOR-CLAIMED.
        issue = {
            "number": 1747,
            "title": "test: test_ssl.py and test_socket.py fail under Python 3.12",
            "body": "I would like to be assigned to this issue to prepare a clean PR",
            "labels": [],
            "assignees": [],
            "user": {"login": "reporter42"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        comments = [
            {"user": {"login": "reporter42"}, "body": "Could you please assign this issue to me?"},
            {"user": {"login": "reporter42"}, "body": "I have a version ready whenever you want it."},
        ]
        config = load_config(Path(__file__).parent.parent / "radar" / "config.yaml")
        result = triage_issue(
            issue, comments, [], REPO,
            positive_keywords=config.positive_keywords,
            negative_keywords=config.negative_keywords,
            claim_phrases=config.claim_phrases,
            reserved_labels=config.reserved_labels,
        )
        assert result["status"] == "AUTHOR-CLAIMED"
        assert result["claim_comments"] == 0
        assert result["contested"] is False

    def test_nettacker_1758_full_scenario_is_author_claimed(self):
        issue = {
            "number": 1758,
            "title": "docs: fix outdated tcp_connect_port_scan references in Usage.md",
            "body": "If this looks good, I can make the documentation change and submit a PR.",
            "labels": [],
            "assignees": [],
            "user": {"login": "jyotish6699"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        config = load_config(Path(__file__).parent.parent / "radar" / "config.yaml")
        result = triage_issue(
            issue, [], [], REPO,
            positive_keywords=config.positive_keywords,
            negative_keywords=config.negative_keywords,
            claim_phrases=config.claim_phrases,
            reserved_labels=config.reserved_labels,
        )
        assert result["status"] == "AUTHOR-CLAIMED"

    def test_single_non_author_claim_comment_is_comment_claimed_not_open_free(self):
        issue = {
            "number": 8,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "user": {"login": "reporter"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        comments = [{"user": {"login": "someone-else"}, "body": "I'll take this"}]
        result = triage_issue(
            issue, comments, [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=["i'll take"], reserved_labels=[],
        )
        assert result["status"] == "COMMENT-CLAIMED"
        assert result["claim_comments"] == 1

    def test_two_non_author_claim_comments_still_contested(self):
        issue = {
            "number": 9,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "user": {"login": "reporter"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        comments = [
            {"user": {"login": "person-a"}, "body": "i'll take"},
            {"user": {"login": "person-b"}, "body": "i'll take"},
        ]
        result = triage_issue(
            issue, comments, [], REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=["i'll take"], reserved_labels=[],
        )
        assert result["status"] == "CONTESTED"

    def test_comment_mentioning_open_pr_is_has_pr(self):
        issue = {
            "number": 10,
            "title": "x",
            "body": "",
            "labels": [],
            "assignees": [],
            "user": {"login": "reporter"},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        timeline = [comment_mention_event(1259, state="open")]
        result = triage_issue(
            issue, [], timeline, REPO,
            positive_keywords=[], negative_keywords=[], claim_phrases=[], reserved_labels=[],
        )
        assert result["status"] == "HAS-PR"
