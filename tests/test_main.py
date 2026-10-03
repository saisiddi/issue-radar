import json
import os
from datetime import datetime, timedelta, timezone

from radar.config import Config, LimitsConfig, LLMConfig, NotifierConfig, PollConfig, RepoConfig
from radar.github_client import BackoffBudgetExceeded, GitHubAPIError
from radar.main import GITHUB_TIMESTAMP_FORMAT, run_poll
from radar.state import load_state

NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)
NOW_STR = NOW.strftime(GITHUB_TIMESTAMP_FORMAT)


def make_config(repos, notifier_type="telegram", first_run_window_days=3):
    return Config(
        repos=repos,
        positive_keywords=["python", "security"],
        negative_keywords=["react"],
        claim_phrases=["assign me", "i'll take"],
        reserved_labels=["gsoc-idea"],
        staleness_days_threshold=30,
        notifier=NotifierConfig(type=notifier_type, dry_run=False),
        llm=LLMConfig(),
        limits=LimitsConfig(max_total_backoff_seconds=300, max_api_calls_per_run=None),
        poll=PollConfig(first_run_window_days=first_run_window_days),
        state_file="state.json",
        sweep_report_file="sweep_report.md",
    )


def make_issue(number, assignees=None, labels=None, updated_at="2026-01-01T00:00:00Z"):
    return {
        "number": number,
        "title": f"Issue {number}: python security bug",
        "body": "",
        "labels": labels or [],
        "assignees": assignees or [],
        "updated_at": updated_at,
        "created_at": updated_at,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "user": {"login": "someone"},
    }


class FakeGitHubClient:
    def __init__(self, issues_by_repo=None, raise_on_list_issues=None, raise_on_detail=None, **_ignored):
        self.issues_by_repo = issues_by_repo or {}
        self.raise_on_list_issues = raise_on_list_issues or {}
        self.raise_on_detail = raise_on_detail or {}
        self.calls = []
        self.request_count = 0

    def list_issues(self, repo, since=None, state="open"):
        self.calls.append(("list_issues", repo, since))
        if repo in self.raise_on_list_issues:
            raise self.raise_on_list_issues[repo]
        return iter(self.issues_by_repo.get(repo, []))

    def list_issue_comments(self, repo, number):
        self.calls.append(("list_issue_comments", repo, number))
        if (repo, number) in self.raise_on_detail:
            raise self.raise_on_detail[(repo, number)]
        return iter([])

    def list_issue_timeline(self, repo, number):
        self.calls.append(("list_issue_timeline", repo, number))
        return iter([])


def patch_client(monkeypatch, fake_client):
    monkeypatch.setattr("radar.main.GitHubClient", lambda **kwargs: fake_client)


class TestRunPollDryRun:
    def test_open_free_issue_is_alerted_and_state_updated(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=["alice"])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1)]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 0
        out = capsys.readouterr().out
        assert "#1" in out
        assert "OPEN-FREE" in out

        state = load_state(state_path)
        repo_state = state.for_repo("owner/repo")
        assert repo_state.alerted_issue_numbers == [1]
        # First-ever poll advances straight to "now", not the issue's own
        # (older) updated_at - see TestFirstRunBootstrap for why.
        assert repo_state.last_seen == NOW_STR

    def test_claimed_issue_is_not_alerted_but_last_seen_still_advances(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [make_issue(1, assignees=[{"login": "x"}])]}
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 0
        assert capsys.readouterr().out == ""
        state = load_state(state_path)
        repo_state = state.for_repo("owner/repo")
        assert repo_state.alerted_issue_numbers == []
        assert repo_state.last_seen == NOW_STR

    def test_already_alerted_issue_is_skipped_without_detail_calls(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1)]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"owner/repo": {"last_seen": None, "alerted_issue_numbers": [1]}}))

        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        detail_calls = [c for c in fake_client.calls if c[0] in ("list_issue_comments", "list_issue_timeline")]
        assert detail_calls == []

    def test_passes_last_seen_as_since_on_next_run(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"owner/repo": {"last_seen": "2026-01-15T00:00:00Z", "alerted_issue_numbers": []}})
        )

        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        list_issues_call = next(c for c in fake_client.calls if c[0] == "list_issues")
        assert list_issues_call[2] == "2026-01-15T00:00:00Z"


class TestFirstRunBootstrap:
    def test_first_run_since_is_now_minus_window_not_unbounded(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo], first_run_window_days=3)
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"  # no state file -> last_seen is None
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        list_issues_call = next(c for c in fake_client.calls if c[0] == "list_issues")
        expected_since = (NOW - timedelta(days=3)).strftime(GITHUB_TIMESTAMP_FORMAT)
        assert list_issues_call[2] == expected_since
        assert list_issues_call[2] is not None  # never unbounded/"since the beginning"

    def test_first_run_window_is_configurable(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo], first_run_window_days=7)
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        list_issues_call = next(c for c in fake_client.calls if c[0] == "list_issues")
        expected_since = (NOW - timedelta(days=7)).strftime(GITHUB_TIMESTAMP_FORMAT)
        assert list_issues_call[2] == expected_since

    def test_first_run_with_zero_issues_still_advances_last_seen_to_now(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        repo_state = load_state(state_path).for_repo("owner/repo")
        assert repo_state.last_seen == NOW_STR

    def test_second_run_no_longer_uses_bootstrap_window(self, monkeypatch, tmp_path):
        # After a first run sets last_seen, later runs must use that
        # timestamp, not re-derive a fresh now-minus-window every time.
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo], first_run_window_days=3)
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"owner/repo": {"last_seen": "2026-01-20T00:00:00Z", "alerted_issue_numbers": []}})
        )

        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        list_issues_call = next(c for c in fake_client.calls if c[0] == "list_issues")
        assert list_issues_call[2] == "2026-01-20T00:00:00Z"


class TestRunPollErrorHandling:
    def test_generic_api_error_skips_repo_and_continues(self, monkeypatch, tmp_path, capsys):
        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config = make_config([repo1, repo2])
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo2": [make_issue(2)]},
            raise_on_list_issues={"owner/repo1": GitHubAPIError("boom", 404)},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 1  # had_error, but still processed repo2
        out = capsys.readouterr().out
        assert "#2" in out

    def test_backoff_budget_exceeded_stops_remaining_repos(self, monkeypatch, tmp_path, capsys):
        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config = make_config([repo1, repo2])
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo2": [make_issue(2)]},
            raise_on_list_issues={"owner/repo1": BackoffBudgetExceeded("budget blown")},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 1
        repo2_calls = [c for c in fake_client.calls if c[1] == "owner/repo2"]
        assert repo2_calls == []  # never reached


class TestLoadDotenv:
    def test_loads_keys_into_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SOME_TEST_VAR", raising=False)
        dotenv_path = tmp_path / ".env"
        dotenv_path.write_text("SOME_TEST_VAR=hello\n# a comment\n\nQUOTED_VAR=\"quoted value\"\n")

        from radar.main import load_dotenv

        load_dotenv(dotenv_path)

        assert os.environ["SOME_TEST_VAR"] == "hello"
        assert os.environ["QUOTED_VAR"] == "quoted value"

    def test_real_environment_takes_precedence_over_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOME_TEST_VAR", "from-real-env")
        dotenv_path = tmp_path / ".env"
        dotenv_path.write_text("SOME_TEST_VAR=from-dotenv\n")

        from radar.main import load_dotenv

        load_dotenv(dotenv_path)

        assert os.environ["SOME_TEST_VAR"] == "from-real-env"

    def test_missing_dotenv_file_is_a_noop(self, tmp_path):
        from radar.main import load_dotenv

        load_dotenv(tmp_path / "does_not_exist.env")  # must not raise


class TestRunPollNotifierConfig:
    def test_missing_telegram_creds_returns_error_before_any_fetch(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo], notifier_type="telegram")
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1)]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=False, token="tok", now=NOW)

        assert rc == 1
        assert fake_client.calls == []
