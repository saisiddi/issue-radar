import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from radar.config import Config, LimitsConfig, LLMConfig, NotifierConfig, PollConfig, RepoConfig, SweepConfig
from radar.github_client import BackoffBudgetExceeded, GitHubAPIError
from radar.main import GITHUB_TIMESTAMP_FORMAT, _sweep_report_path, resolve_github_token, run_poll, run_sweep
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
        sweep=SweepConfig(),
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
    def __init__(
        self,
        issues_by_repo=None,
        raise_on_list_issues=None,
        raise_on_detail=None,
        timeline_by_issue=None,
        comments_by_issue=None,
        issue_by_number=None,
        raise_on_get_issue=None,
        **_ignored,
    ):
        self.issues_by_repo = issues_by_repo or {}
        self.raise_on_list_issues = raise_on_list_issues or {}
        self.raise_on_detail = raise_on_detail or {}
        self.timeline_by_issue = timeline_by_issue or {}
        self.comments_by_issue = comments_by_issue or {}
        self.issue_by_number = issue_by_number or {}
        self.raise_on_get_issue = raise_on_get_issue or {}
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
        return iter(self.comments_by_issue.get((repo, number), []))

    def list_issue_timeline(self, repo, number):
        self.calls.append(("list_issue_timeline", repo, number))
        return iter(self.timeline_by_issue.get((repo, number), []))

    def get_issue(self, repo, number):
        self.calls.append(("get_issue", repo, number))
        if (repo, number) in self.raise_on_get_issue:
            raise self.raise_on_get_issue[(repo, number)]
        return self.issue_by_number[(repo, number)]


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


class TestPerRepoOverridesInPoll:
    def test_repo_override_keywords_are_used_instead_of_global(self, monkeypatch, tmp_path, capsys):
        # Global keywords are python/security/react; this repo overrides
        # positive_keywords to "rust" only. An issue that matches "rust"
        # but none of the global keywords should still come out GOOD.
        repo = RepoConfig(
            name="owner/repo",
            org="Org",
            reviewers=[],
            positive_keywords=["rust"],
            negative_keywords=[],
        )
        config = make_config([repo])
        issue = make_issue(1)
        issue["title"] = "Fix a rust memory bug"
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [issue]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        out = capsys.readouterr().out
        assert "GOOD" in out
        assert "rust" in out

    def test_repo_without_override_falls_back_to_global_keywords(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        issue["title"] = "Fix a python security bug"
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [issue]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        out = capsys.readouterr().out
        assert "GOOD" in out
        assert "python" in out


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


class _FakeDatetimeNamespace:
    """Stands in for the `datetime` name inside radar.main: forwards
    everything except `.now()`, which returns one scripted value and
    counts how many times it was called."""

    def __init__(self, now_value):
        self.now_value = now_value
        self.now_call_count = 0

    def now(self, tz=None):
        self.now_call_count += 1
        return self.now_value


class TestRunStartTimeNotEndTime:
    def test_now_is_computed_exactly_once_per_run(self, monkeypatch, tmp_path):
        import radar.main as main_module

        fake_datetime = _FakeDatetimeNamespace(NOW)
        monkeypatch.setattr(main_module, "datetime", fake_datetime)

        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        # Two issues needing detail calls, so there's real work for a
        # regression to sneak a second now() call into.
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1), make_issue(2)]})
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok")  # now=None -> internal clock read

        assert fake_datetime.now_call_count == 1

    def test_run_start_time_is_a_floor_not_a_ceiling(self, monkeypatch, tmp_path):
        # `now` (the run's start time) only seeds latest_seen as a lower
        # bound; it must never clamp it down below an issue's own
        # updated_at. Otherwise a fast-moving issue fetched mid-run could
        # get its advancement capped at start time instead of its real
        # updated_at, and the next poll would redundantly re-fetch it.
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        run_start = NOW
        issue_updated_after_start = (run_start + timedelta(minutes=1)).strftime(GITHUB_TIMESTAMP_FORMAT)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [make_issue(1, updated_at=issue_updated_after_start)]}
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"owner/repo": {"last_seen": "2026-01-20T00:00:00Z", "alerted_issue_numbers": []}})
        )
        run_poll(config, str(state_path), dry_run=True, token="tok", now=run_start)

        repo_state = load_state(state_path).for_repo("owner/repo")
        assert repo_state.last_seen == issue_updated_after_start


class TestCommentClaimedStatus:
    def test_single_non_author_claim_comment_not_alerted_in_poll(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "someone-else"}, "body": "i'll take"}]},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 0
        assert capsys.readouterr().out == ""  # COMMENT-CLAIMED is not poll-alertable

    def test_comment_claimed_issue_kept_in_sweep_report(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "someone-else"}, "body": "i'll take"}]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "#1" in content
        assert "COMMENT-CLAIMED" in content

    def test_two_non_author_claims_still_contested_and_not_in_poll(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={
                ("owner/repo", 1): [
                    {"user": {"login": "person-a"}, "body": "i'll take"},
                    {"user": {"login": "person-b"}, "body": "i'll take"},
                ]
            },
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        out = capsys.readouterr().out
        assert "CONTESTED" in out  # 2+ claims still alerts as CONTESTED, unlike exactly 1


class TestCommentMentionedPRs:
    def test_poll_resolves_mention_and_skips_has_pr_issue(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "x"}, "body": "opened PR #1259 for this"}]},
            issue_by_number={
                ("owner/repo", 1259): {
                    "number": 1259,
                    "state": "open",
                    "title": "",
                    "body": "",
                    "pull_request": {"url": "...", "html_url": "...", "merged_at": None},
                }
            },
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 0
        assert capsys.readouterr().out == ""  # HAS-PR is never poll-alertable
        assert ("get_issue", "owner/repo", 1259) in fake_client.calls

    def test_sweep_shows_has_pr_from_comment_mention_is_excluded(self, monkeypatch, tmp_path):
        # HAS-PR is excluded from the sweep report too (same as CLAIMED) -
        # confirm the resolved mention actually drives that exclusion,
        # rather than the issue just happening to be OPEN-FREE anyway.
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "x"}, "body": "see #1259"}]},
            issue_by_number={
                ("owner/repo", 1259): {
                    "number": 1259,
                    "state": "open",
                    "title": "",
                    "body": "",
                    "pull_request": {"url": "...", "html_url": "...", "merged_at": None},
                }
            },
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "#1 " not in content and "[#1]" not in content

    def test_unresolvable_mention_is_skipped_not_fatal(self, monkeypatch, tmp_path, capsys):
        from radar.github_client import GitHubAPIError

        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "x"}, "body": "see #999999"}]},
            raise_on_get_issue={("owner/repo", 999999): GitHubAPIError("not found", 404)},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 0
        out = capsys.readouterr().out
        assert "#1" in out
        assert "OPEN-FREE" in out  # unresolvable mention didn't block normal triage

    def test_mention_resolving_to_a_plain_issue_not_a_pr_is_ignored(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "x"}, "body": "related to #42"}]},
            issue_by_number={("owner/repo", 42): {"number": 42, "state": "open", "title": "", "body": ""}},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        out = capsys.readouterr().out
        assert "OPEN-FREE" in out  # #42 has no pull_request key - not a PR, ignored

    def test_budget_exceeded_during_mention_resolution_propagates(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "x"}, "body": "see #1259"}]},
            raise_on_get_issue={("owner/repo", 1259): BackoffBudgetExceeded("budget blown")},
        )
        patch_client(monkeypatch, fake_client)

        state_path = tmp_path / "state.json"
        rc = run_poll(config, str(state_path), dry_run=True, token="tok", now=NOW)

        assert rc == 1


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


def _fake_gh_run(returncode=0, stdout="", raise_exc=None):
    def _run(args, capture_output=None, text=None, timeout=None):
        if raise_exc:
            raise raise_exc
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return _run


class TestResolveGithubToken:
    def test_prefers_gh_cli_when_logged_in(self, monkeypatch):
        monkeypatch.setattr("radar.main.subprocess.run", _fake_gh_run(returncode=0, stdout="ghs_fake123\n"))
        monkeypatch.setenv("GITHUB_TOKEN", "should-not-be-used")

        token, source = resolve_github_token()

        assert token == "ghs_fake123"
        assert source == "gh CLI"

    def test_falls_back_to_github_token_when_gh_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            "radar.main.subprocess.run", _fake_gh_run(raise_exc=FileNotFoundError("gh not found"))
        )
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")
        monkeypatch.delenv("GH_PAT", raising=False)

        token, source = resolve_github_token()

        assert token == "from-env"
        assert source == "GITHUB_TOKEN env var"

    def test_falls_back_to_gh_pat_when_github_token_missing(self, monkeypatch):
        monkeypatch.setattr(
            "radar.main.subprocess.run", _fake_gh_run(raise_exc=FileNotFoundError("gh not found"))
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_PAT", "from-gh-pat")

        token, source = resolve_github_token()

        assert token == "from-gh-pat"
        assert source == "GH_PAT env var"

    def test_falls_back_when_gh_cli_not_logged_in(self, monkeypatch):
        # gh installed but `gh auth token` fails (not logged in) -> non-zero exit.
        monkeypatch.setattr("radar.main.subprocess.run", _fake_gh_run(returncode=1, stdout=""))
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")

        token, source = resolve_github_token()

        assert token == "from-env"
        assert source == "GITHUB_TOKEN env var"

    def test_falls_back_when_gh_cli_returns_empty_output(self, monkeypatch):
        monkeypatch.setattr("radar.main.subprocess.run", _fake_gh_run(returncode=0, stdout="   \n"))
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")

        token, source = resolve_github_token()

        assert token == "from-env"

    def test_returns_none_when_nothing_available(self, monkeypatch):
        monkeypatch.setattr(
            "radar.main.subprocess.run", _fake_gh_run(raise_exc=FileNotFoundError("gh not found"))
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_PAT", raising=False)

        token, source = resolve_github_token()

        assert token is None
        assert source is None

    def test_never_prints_the_token_value(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "radar.main.subprocess.run", _fake_gh_run(returncode=0, stdout="super-secret-token\n")
        )

        token, _ = resolve_github_token()

        assert token == "super-secret-token"
        captured = capsys.readouterr()
        assert "super-secret-token" not in captured.out
        assert "super-secret-token" not in captured.err


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


def make_closing_pr_event(pr_number, target_issue_number, repo_full_name="owner/repo", state="open", merged_at=None):
    return {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": pr_number,
                "state": state,
                "title": "",
                "body": f"Closes #{target_issue_number}",
                "repository": {"full_name": repo_full_name},
                "html_url": f"https://github.com/{repo_full_name}/pull/{pr_number}",
                "pull_request": {
                    "url": f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
                    "html_url": f"https://github.com/{repo_full_name}/pull/{pr_number}",
                    "merged_at": merged_at,
                },
            },
        },
    }


class TestRunSweep:
    def test_report_written_grouped_and_sorted(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])

        good_issue = make_issue(1)  # title has "python security bug" -> GOOD fit
        maybe_issue = make_issue(2)
        maybe_issue["title"] = "Improve something unrelated"
        maybe_issue["body"] = ""

        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [maybe_issue, good_issue]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        rc = run_sweep(config, dry_run=False, token="tok", now=NOW)

        assert rc == 0
        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "owner/repo" in content
        # GOOD-fit issue must be listed ahead of the MAYBE-fit one.
        assert content.index("#1") < content.index("#2")

    def test_skips_claimed_and_has_pr(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])

        claimed = make_issue(1, assignees=[{"login": "x"}])
        has_pr = make_issue(2)
        free = make_issue(3)

        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [claimed, has_pr, free]},
            timeline_by_issue={("owner/repo", 2): [make_closing_pr_event(99, 2)]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "#1" not in content  # CLAIMED
        assert "#2" not in content  # HAS-PR
        assert "#3" in content  # OPEN-FREE

    def test_likely_already_fixed_column(self, monkeypatch, tmp_path):
        # Same-repo merged PR but no closing keyword -> UNSURE, kept in the
        # report, flagged as likely already fixed.
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        issue = make_issue(5)
        timeline_event = make_closing_pr_event(50, target_issue_number=999, state="closed", merged_at="2026-01-01T00:00:00Z")
        # body references #5 but without a closing keyword
        timeline_event["source"]["issue"]["body"] = "see #5"

        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            timeline_by_issue={("owner/repo", 5): [timeline_event]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "#5" in content
        assert "UNSURE" in content
        lines = [l for l in content.splitlines() if "#5" in l]
        assert "Yes" in lines[0]

    def test_repo_filter_restricts_to_named_repo(self, monkeypatch, tmp_path):
        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config = make_config([repo1, repo2])

        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo1": [make_issue(1)], "owner/repo2": [make_issue(2)]}
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", repo_names=["owner/repo1"], now=NOW)

        repo_calls = {c[1] for c in fake_client.calls if c[0] == "list_issues"}
        assert repo_calls == {"owner/repo1"}

    def test_unknown_repo_filter_warns_but_does_not_crash(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        rc = run_sweep(config, dry_run=False, token="tok", repo_names=["owner/does-not-exist"], now=NOW)

        assert rc == 0
        assert "does-not-exist" in capsys.readouterr().err

    def test_budget_exceeded_stops_remaining_repos(self, monkeypatch, tmp_path):
        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config = make_config([repo1, repo2])
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo2": [make_issue(2)]},
            raise_on_list_issues={"owner/repo1": BackoffBudgetExceeded("budget blown")},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        rc = run_sweep(config, dry_run=False, token="tok", now=NOW)

        assert rc == 1
        repo2_calls = [c for c in fake_client.calls if c[1] == "owner/repo2"]
        assert repo2_calls == []

    def test_generic_error_skips_repo_and_continues(self, monkeypatch, tmp_path):
        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config = make_config([repo1, repo2])
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo2": [make_issue(2)]},
            raise_on_list_issues={"owner/repo1": GitHubAPIError("boom", 404)},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        rc = run_sweep(config, dry_run=False, token="tok", now=NOW)

        assert rc == 1
        content = _sweep_report_path(report_path, "owner/repo2").read_text()
        assert "#2" in content

    def test_dry_run_prints_top_rows(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1)]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=True, token="tok", now=NOW)

        out = capsys.readouterr().out
        assert "#1" in out
        assert "OPEN-FREE" in out

    def test_sweep_uses_since_none_fetches_all_open_issues(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": []})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        call = next(c for c in fake_client.calls if c[0] == "list_issues")
        assert call[2] is None  # sweep is a full scan, not incremental

    def test_separate_runs_on_different_repos_do_not_overwrite_each_other(self, monkeypatch, tmp_path):
        base_report_path = tmp_path / "sweep_report.md"

        repo1 = RepoConfig(name="owner/repo1", org="Org", reviewers=[])
        config1 = make_config([repo1])
        config1.sweep_report_file = str(base_report_path)
        fake_client1 = FakeGitHubClient(issues_by_repo={"owner/repo1": [make_issue(1)]})
        patch_client(monkeypatch, fake_client1)
        run_sweep(config1, dry_run=False, token="tok", now=NOW)

        repo2 = RepoConfig(name="owner/repo2", org="Org", reviewers=[])
        config2 = make_config([repo2])
        config2.sweep_report_file = str(base_report_path)
        fake_client2 = FakeGitHubClient(issues_by_repo={"owner/repo2": [make_issue(2)]})
        patch_client(monkeypatch, fake_client2)
        run_sweep(config2, dry_run=False, token="tok", now=NOW)

        # Both reports must still exist and contain their own repo's issue -
        # the second run must not have clobbered the first's file.
        report1 = _sweep_report_path(base_report_path, "owner/repo1")
        report2 = _sweep_report_path(base_report_path, "owner/repo2")
        assert report1.exists() and report2.exists()
        assert "#1" in report1.read_text()
        assert "#2" in report2.read_text()
        assert "#2" not in report1.read_text()

    def test_maintainer_replied_column_reflects_reviewer_comment(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=["securestep9"])
        config = make_config([repo])
        issue = make_issue(1)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [issue]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "securestep9"}, "body": "looks good"}]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        line = next(l for l in _sweep_report_path(report_path, "owner/repo").read_text().splitlines() if "#1" in l)
        # columns: # | Title | Status | Fit | Staleness | Maintainer replied? | Likely already fixed?
        assert line.split("|")[6].strip() == "Yes"

    def test_very_old_unanswered_issue_goes_to_separate_section(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=["securestep9"])
        config = make_config([repo])
        very_old = make_issue(1, updated_at="2025-01-01T00:00:00Z")  # ~13 months before NOW
        fresh = make_issue(2, updated_at="2026-01-25T00:00:00Z")  # a week before NOW
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [very_old, fresh]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        main_section, _, old_section = content.partition("### Old / unanswered")
        assert "#2" in main_section
        assert "#1" not in main_section
        assert "#1" in old_section

    def test_maintainer_replied_keeps_old_issue_out_of_unanswered_section(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=["securestep9"])
        config = make_config([repo])
        very_old_but_answered = make_issue(1, updated_at="2025-01-01T00:00:00Z")
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [very_old_but_answered]},
            comments_by_issue={("owner/repo", 1): [{"user": {"login": "securestep9"}, "body": "on it"}]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "### Old / unanswered" not in content
        assert "#1" in content

    def test_sort_good_fit_before_maybe_fit(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        good = make_issue(1)  # title matches python/security -> GOOD
        maybe = make_issue(2)
        maybe["title"] = "Something unrelated"
        maybe["body"] = ""
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [maybe, good]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert content.index("#1") < content.index("#2")

    def test_sort_maintainer_replied_before_not_within_same_fit(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=["securestep9"])
        config = make_config([repo])
        # Both GOOD fit (title matches python/security); #2 has a
        # maintainer reply and should sort ahead of #1 despite a higher
        # issue number.
        no_reply = make_issue(1)
        replied = make_issue(2)
        fake_client = FakeGitHubClient(
            issues_by_repo={"owner/repo": [no_reply, replied]},
            comments_by_issue={("owner/repo", 2): [{"user": {"login": "securestep9"}, "body": "on it"}]},
        )
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert content.index("#2") < content.index("#1")

    def test_sort_most_recent_activity_first_within_same_tier(self, monkeypatch, tmp_path):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        older = make_issue(1, updated_at="2026-01-01T00:00:00Z")
        newer = make_issue(2, updated_at="2026-01-30T00:00:00Z")
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [older, newer]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert content.index("#2") < content.index("#1")  # more recent (#2) first

    def test_pr_exclusion_clarified_in_output(self, monkeypatch, tmp_path, capsys):
        repo = RepoConfig(name="owner/repo", org="Org", reviewers=[])
        config = make_config([repo])
        fake_client = FakeGitHubClient(issues_by_repo={"owner/repo": [make_issue(1)]})
        patch_client(monkeypatch, fake_client)

        report_path = tmp_path / "sweep_report.md"
        config.sweep_report_file = str(report_path)
        run_sweep(config, dry_run=False, token="tok", now=NOW)

        err = capsys.readouterr().err
        assert "pull requests excluded" in err.lower()
        assert "1" in err  # scanned count

        report_content = _sweep_report_path(report_path, "owner/repo").read_text()
        assert "open_issues_count" in report_content


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
