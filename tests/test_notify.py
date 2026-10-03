from datetime import datetime, timezone

import pytest

from radar.config import RepoConfig
from radar.notify import (
    DiscordNotifier,
    DryRunNotifier,
    NotifierConfigError,
    NotifierError,
    TelegramNotifier,
    format_alert,
    get_notifier,
)

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)

REPO_CFG = RepoConfig(name="owner/repo", org="OrgName", reviewers=["alice", "bob"])


def make_issue(**overrides):
    issue = {
        "number": 42,
        "title": "Add a scanner module",
        "body": "Line one.\nLine two.\nLine three.\nLine four (should be cut off).",
        "labels": [{"name": "bug"}, {"name": "security"}],
        "user": {"login": "someuser"},
        "html_url": "https://github.com/owner/repo/issues/42",
        "created_at": "2026-01-01T00:00:00Z",
    }
    issue.update(overrides)
    return issue


def make_triage(**overrides):
    triage = {
        "number": 42,
        "title": "Add a scanner module",
        "status": "OPEN-FREE",
        "fit_tag": "GOOD",
        "matched_keywords": ["python", "security"],
        "claim_comments": 0,
        "linked_pr_notes": [],
        "reserved_labels": [],
    }
    triage.update(overrides)
    return triage


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.response


class TestFormatAlert:
    def test_includes_core_fields(self):
        message = format_alert(make_triage(), make_issue(), REPO_CFG)
        assert "owner/repo" in message
        assert "org: OrgName" in message
        assert "#42" in message
        assert "Add a scanner module" in message
        assert "https://github.com/owner/repo/issues/42" in message
        assert "bug, security" in message
        assert "someuser" in message
        assert "GOOD" in message
        assert "python, security" in message
        assert "alice, bob" in message

    def test_body_excerpt_capped_at_three_lines(self):
        message = format_alert(make_triage(), make_issue(), REPO_CFG)
        assert "Line one." in message
        assert "Line two." in message
        assert "Line three." in message
        assert "Line four" not in message

    def test_fixed_footer_present_and_no_comment_template(self):
        message = format_alert(make_triage(), make_issue(), REPO_CFG)
        assert "Read the issue and the code before commenting. Ask for assignment with a specific plan." in message
        assert "please assign me" not in message.lower()

    def test_unsure_status_includes_reason(self):
        triage = make_triage(
            status="UNSURE",
            linked_pr_notes=["PR #7 (open) mentions this issue but has no closing keyword"],
        )
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Why UNSURE:" in message
        assert "PR #7 (open) mentions this issue but has no closing keyword" in message

    def test_unsure_status_with_no_notes_still_explains(self):
        triage = make_triage(status="UNSURE", linked_pr_notes=[])
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Why UNSURE:" in message

    def test_fork_unsure_reason_included(self):
        triage = make_triage(
            status="UNSURE",
            linked_pr_notes=["possible work in progress elsewhere: PR #9 in a-fork/repo (https://...)"],
        )
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "possible work in progress elsewhere: PR #9 in a-fork/repo" in message

    def test_discuss_only_includes_reserved_label(self):
        triage = make_triage(status="DISCUSS-ONLY", reserved_labels=["gsoc-idea"])
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Reserved label: gsoc-idea" in message

    def test_contested_includes_claim_count(self):
        triage = make_triage(status="CONTESTED", claim_comments=3)
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Claim comments: 3" in message

    def test_abandoned_attempt_note_surfaced_even_when_open_free(self):
        triage = make_triage(status="OPEN-FREE", linked_pr_notes=["previous attempt #99 closed unmerged"])
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Note: previous attempt #99 closed unmerged" in message

    def test_no_reviewers_listed(self):
        repo_cfg = RepoConfig(name="x/y", org="Z", reviewers=[])
        message = format_alert(make_triage(), make_issue(), repo_cfg)
        assert "none listed" in message

    def test_likely_already_fixed_line_shown_for_unsure(self):
        triage = make_triage(
            status="UNSURE",
            linked_pr_notes=["PR #7 (merged) mentions this issue but has no closing keyword"],
            likely_already_fixed=True,
        )
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Likely already fixed: a merged same-repo PR already references this issue" in message

    def test_likely_already_fixed_line_absent_when_false(self):
        triage = make_triage(status="UNSURE", linked_pr_notes=["some reason"], likely_already_fixed=False)
        message = format_alert(triage, make_issue(), REPO_CFG)
        assert "Likely already fixed" not in message


class TestOldIssueNote:
    def test_shown_when_older_than_threshold(self):
        issue = make_issue(created_at="2026-01-01T00:00:00Z", updated_at="2026-02-25T00:00:00Z")
        message = format_alert(make_triage(), issue, REPO_CFG, staleness_days_threshold=30, now=NOW)
        assert "Old issue (created 59 days ago), recent activity on 2026-02-25" in message

    def test_absent_when_within_threshold(self):
        issue = make_issue(created_at="2026-02-15T00:00:00Z", updated_at="2026-02-25T00:00:00Z")
        message = format_alert(make_triage(), issue, REPO_CFG, staleness_days_threshold=30, now=NOW)
        assert "Old issue" not in message

    def test_absent_exactly_at_threshold(self):
        # created exactly 30 days before NOW - "older than 30 days" means
        # strictly more, not inclusive.
        issue = make_issue(created_at="2026-01-30T00:00:00Z", updated_at="2026-02-25T00:00:00Z")
        message = format_alert(make_triage(), issue, REPO_CFG, staleness_days_threshold=30, now=NOW)
        assert "Old issue" not in message

    def test_respects_custom_threshold(self):
        issue = make_issue(created_at="2026-02-10T00:00:00Z", updated_at="2026-02-25T00:00:00Z")
        message = format_alert(make_triage(), issue, REPO_CFG, staleness_days_threshold=10, now=NOW)
        assert "Old issue" in message

    def test_absent_when_no_created_at(self):
        issue = make_issue(created_at=None)
        message = format_alert(make_triage(), issue, REPO_CFG, staleness_days_threshold=30, now=NOW)
        assert "Old issue" not in message


class TestDryRunNotifier:
    def test_prints_message(self, capsys):
        DryRunNotifier().send("hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.out


class TestTelegramNotifier:
    def test_sends_to_correct_url_and_payload(self):
        session = FakeSession(FakeResponse(status_code=200))
        notifier = TelegramNotifier("tok123", "chat456", session=session)
        notifier.send("hi")

        call = session.calls[0]
        assert call["url"] == "https://api.telegram.org/bottok123/sendMessage"
        assert call["json"]["chat_id"] == "chat456"
        assert call["json"]["text"] == "hi"

    def test_truncates_long_messages(self):
        session = FakeSession(FakeResponse(status_code=200))
        notifier = TelegramNotifier("tok", "chat", session=session)
        notifier.send("x" * 5000)
        assert len(session.calls[0]["json"]["text"]) == 4096

    def test_raises_on_failure(self):
        session = FakeSession(FakeResponse(status_code=400, text="bad request"))
        notifier = TelegramNotifier("tok", "chat", session=session)
        with pytest.raises(NotifierError):
            notifier.send("hi")

    def test_does_not_leak_token_in_error(self):
        session = FakeSession(FakeResponse(status_code=400, text="bad request"))
        notifier = TelegramNotifier("super-secret-token", "chat", session=session)
        with pytest.raises(NotifierError) as exc_info:
            notifier.send("hi")
        assert "super-secret-token" not in str(exc_info.value)


class TestDiscordNotifier:
    def test_sends_content_payload(self):
        session = FakeSession(FakeResponse(status_code=204))
        notifier = DiscordNotifier("https://discord.example/webhook", session=session)
        notifier.send("hi")

        call = session.calls[0]
        assert call["url"] == "https://discord.example/webhook"
        assert call["json"]["content"] == "hi"

    def test_raises_on_failure(self):
        session = FakeSession(FakeResponse(status_code=500, text="server error"))
        notifier = DiscordNotifier("https://discord.example/webhook", session=session)
        with pytest.raises(NotifierError):
            notifier.send("hi")


class TestGetNotifier:
    def test_dry_run_overrides_type(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        notifier = get_notifier("telegram", dry_run=True)
        assert isinstance(notifier, DryRunNotifier)

    def test_telegram_requires_env_vars(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(NotifierConfigError):
            get_notifier("telegram", dry_run=False)

    def test_telegram_with_env_vars(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
        notifier = get_notifier("telegram", dry_run=False)
        assert isinstance(notifier, TelegramNotifier)

    def test_discord_requires_webhook(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        with pytest.raises(NotifierConfigError):
            get_notifier("discord", dry_run=False)

    def test_discord_with_webhook(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
        notifier = get_notifier("discord", dry_run=False)
        assert isinstance(notifier, DiscordNotifier)

    def test_unknown_type_raises(self):
        with pytest.raises(NotifierConfigError):
            get_notifier("carrier-pigeon", dry_run=False)
