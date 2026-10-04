from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar.config import Config, effective_keywords, load_config
from radar.github_client import (
    BackoffBudgetExceeded,
    GitHubAPIError,
    GitHubClient,
    RequestBudgetExceeded,
)
from radar.notify import NotifierConfigError, NotifierError, format_alert, get_notifier
from radar.state import load_state, save_state
from radar.triage import extract_comment_pr_mentions, maintainer_replied, triage_issue

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
DEFAULT_DOTENV_PATH = Path(__file__).parent.parent / ".env"
# Statuses poll will actually alert on. CLAIMED and HAS-PR are excluded -
# someone's already on it. COMMENT-CLAIMED is also excluded here: a single
# non-author claim comment is worth noting in a sweep report, but not worth
# an interrupt-driven alert.
POLL_ALERTABLE_STATUSES = {"OPEN-FREE", "CONTESTED", "UNSURE", "DISCUSS-ONLY", "AUTHOR-CLAIMED"}
# Sweep keeps everything poll would alert on, plus COMMENT-CLAIMED - a sweep
# is a deliberate backlog read, so it's worth surfacing there even though
# it's not alert-worthy on its own.
SWEEP_KEPT_STATUSES = POLL_ALERTABLE_STATUSES | {"COMMENT-CLAIMED"}
GITHUB_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def load_dotenv(path: Path = DEFAULT_DOTENV_PATH) -> None:
    """Load KEY=VALUE pairs from .env into the environment. Never logs values.

    Real environment variables already set take precedence (setdefault).
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _token_from_gh_cli() -> str | None:
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None  # gh not installed, or failed to launch
    if result.returncode != 0:
        return None  # gh installed but not logged in (or other gh error)
    token = result.stdout.strip()
    return token or None


def resolve_github_token() -> tuple[str | None, str | None]:
    """Resolve a GitHub token, preferring `gh auth token` over env vars.

    Returns (token, source_label). source_label is safe to log; the token
    itself never is.
    """
    gh_token = _token_from_gh_cli()
    if gh_token:
        return gh_token, "gh CLI"
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"], "GITHUB_TOKEN env var"
    if os.environ.get("GH_PAT"):
        return os.environ["GH_PAT"], "GH_PAT env var"
    return None, None


def _is_my_issue(issue: dict, my_username: str | None) -> bool:
    if not my_username:
        return False
    author = (issue.get("user") or {}).get("login")
    return bool(author) and author.lower() == my_username.lower()


def _summary_reason(result: dict) -> str:
    if result["status"] == "UNSURE":
        return "; ".join(result.get("linked_pr_notes") or []) or "uncertain signal, check manually"
    if result["status"] == "CONTESTED":
        return f"{result['claim_comments']} claim comments, unassigned"
    if result["status"] == "DISCUSS-ONLY":
        return f"reserved: {', '.join(result.get('reserved_hints') or [])}"
    if result["status"] == "AUTHOR-CLAIMED":
        return f"author self-claim: {result.get('author_claim_reason') or 'unspecified'}"
    abandoned = [n for n in (result.get("linked_pr_notes") or []) if "closed unmerged" in n]
    if abandoned:
        return "; ".join(abandoned)
    return f"fit {result['fit_tag']} (matched: {', '.join(result.get('matched_keywords') or []) or 'none'})"


def _resolve_comment_mentioned_prs(
    client: GitHubClient, repo_name: str, issue_number: int, comments: list[dict]
) -> list[dict]:
    """Fetch each comment-mentioned candidate and, for the ones that
    actually are PRs, return synthetic cross-referenced-shaped timeline
    events ready to merge into this issue's timeline (see
    triage.extract_comment_pr_mentions / analyze_linked_prs).

    A single bad mention (404, deleted, inaccessible) is skipped rather
    than failing the whole issue; a run-level budget exception propagates
    so the caller's existing budget handling still applies.
    """
    synthetic_events = []
    for mention_repo, mention_number in extract_comment_pr_mentions(comments, repo_name, issue_number):
        try:
            mentioned = client.get_issue(mention_repo, mention_number)
        except (BackoffBudgetExceeded, RequestBudgetExceeded):
            raise
        except GitHubAPIError:
            continue  # deleted, inaccessible, or otherwise unresolvable - skip
        if "pull_request" not in mentioned:
            continue  # it's a plain issue, not a PR

        enriched = dict(mentioned)
        enriched["repository"] = {"full_name": mention_repo}
        synthetic_events.append(
            {"event": "cross-referenced", "source": {"type": "issue", "issue": enriched}, "_mention_source": "comment"}
        )
    return synthetic_events


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    common.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")

    parser = argparse.ArgumentParser(
        prog="radar", description="GitHub issue alert and triage tool", parents=[common]
    )

    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("poll", help="Alert on new/changed issues since last run", parents=[common])

    sweep_parser = subparsers.add_parser(
        "sweep", help="One-time report over all open issues", parents=[common]
    )
    sweep_parser.add_argument(
        "--repo", action="append", help="Restrict sweep to this repo (repeatable); default: all configured repos"
    )

    return parser


def run_poll(
    config: Config, state_path: str, dry_run: bool, token: str | None, now: datetime | None = None
) -> int:
    # Captured once, here, before any API call - this is the run's START
    # time. A run with many repos/issues can take minutes; if `now` were
    # computed later (e.g. per-repo or at the end), an issue opened mid-run
    # could fall in the gap and never get alerted on.
    now = now or datetime.now(timezone.utc)
    now_str = now.strftime(GITHUB_TIMESTAMP_FORMAT)

    state = load_state(state_path)
    try:
        notifier = get_notifier(config.notifier.type, dry_run)
    except NotifierConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    client = GitHubClient(
        token=token,
        max_total_backoff_seconds=config.limits.max_total_backoff_seconds,
        max_requests=config.limits.max_api_calls_per_run,
    )

    had_error = False
    summary_rows: list[tuple[str, int, str, str, str, str]] = []

    for repo_cfg in config.repos:
        positive_keywords, negative_keywords, claim_phrases, reserved_labels = effective_keywords(repo_cfg, config)
        repo_state = state.for_repo(repo_cfg.name)
        is_first_run = repo_state.last_seen is None
        since = repo_state.last_seen or (
            now - timedelta(days=config.poll.first_run_window_days)
        ).strftime(GITHUB_TIMESTAMP_FORMAT)

        try:
            issues = list(client.list_issues(repo_cfg.name, since=since, state="open"))
        except (BackoffBudgetExceeded, RequestBudgetExceeded) as e:
            print(f"warning: run budget exceeded, stopping before {repo_cfg.name}: {e}", file=sys.stderr)
            had_error = True
            break
        except GitHubAPIError as e:
            print(f"warning: skipping {repo_cfg.name} after fetch error: {e}", file=sys.stderr)
            had_error = True
            continue

        # On a repo's first-ever poll, advance straight to "now" regardless
        # of what was fetched, so the next poll starts from here forward
        # rather than re-scanning the bootstrap window every 30 minutes.
        latest_seen = now_str if is_first_run else repo_state.last_seen
        budget_exhausted = False

        for issue in issues:
            number = issue["number"]
            updated_at = issue.get("updated_at")
            if updated_at and (latest_seen is None or updated_at > latest_seen):
                latest_seen = updated_at

            if number in repo_state.alerted_issue_numbers:
                continue

            if _is_my_issue(issue, config.my_username):
                continue  # never alert on issues you authored yourself

            try:
                comments = list(client.list_issue_comments(repo_cfg.name, number))
                timeline = list(client.list_issue_timeline(repo_cfg.name, number))
                timeline += _resolve_comment_mentioned_prs(client, repo_cfg.name, number, comments)
            except (BackoffBudgetExceeded, RequestBudgetExceeded) as e:
                print(
                    f"warning: run budget exceeded while triaging {repo_cfg.name}#{number}, stopping: {e}",
                    file=sys.stderr,
                )
                had_error = True
                budget_exhausted = True
                break
            except GitHubAPIError as e:
                print(f"warning: skipping {repo_cfg.name}#{number} after error: {e}", file=sys.stderr)
                had_error = True
                continue

            result = triage_issue(
                issue=issue,
                comments=comments,
                timeline=timeline,
                repo_name=repo_cfg.name,
                positive_keywords=positive_keywords,
                negative_keywords=negative_keywords,
                claim_phrases=claim_phrases,
                reserved_labels=reserved_labels,
            )

            if result["status"] not in POLL_ALERTABLE_STATUSES:
                continue

            message = format_alert(result, issue, repo_cfg, config.staleness_days_threshold, now=now)
            try:
                notifier.send(message)
            except NotifierError as e:
                print(f"warning: failed to send alert for {repo_cfg.name}#{number}: {e}", file=sys.stderr)
                had_error = True
                continue

            repo_state.alerted_issue_numbers.append(number)
            summary_rows.append(
                (repo_cfg.name, number, issue.get("title") or "", result["status"], result["fit_tag"], _summary_reason(result))
            )

        repo_state.last_seen = latest_seen
        save_state(state, state_path)  # persist progressively so a crash doesn't lose earlier repos' progress

        if budget_exhausted:
            break

    if dry_run and summary_rows:
        print()
        print(f"{'repo':<45} {'#':>6} {'status':<12} {'fit':<7} title / reason")
        for repo, number, title, status, fit_tag, reason in summary_rows:
            print(f"{repo:<45} {number:>6} {status:<12} {fit_tag:<7} {title}")
            print(f"{'':<45} {'':>6} {'':<12} {'':<7} -> {reason}")

    print(f"\nAPI requests used this run: {client.request_count}", file=sys.stderr)

    return 1 if had_error else 0


def _is_old_unanswered(row: dict, very_old_days_threshold: int) -> bool:
    staleness = row["staleness_days"]
    return not row.get("maintainer_replied") and staleness is not None and staleness >= very_old_days_threshold


def _main_sort_key(row: dict) -> tuple:
    # GOOD fit first, then issues a maintainer has already weighed in on,
    # then most-recent activity first within each tier.
    staleness = row["staleness_days"] if row["staleness_days"] is not None else 10**9
    return (
        0 if row["fit_tag"] == "GOOD" else 1,
        0 if row.get("maintainer_replied") else 1,
        staleness,
    )


def _old_unanswered_sort_key(row: dict) -> tuple:
    staleness = row["staleness_days"] if row["staleness_days"] is not None else -1
    return -staleness  # most neglected first


def _sweep_table_header() -> str:
    return (
        "| # | Title | Status | Fit | Staleness (days) | Maintainer replied? | Likely already fixed? |\n"
        "|---|---|---|---|---|---|---|"
    )


def _sweep_table_row(row: dict) -> str:
    fixed = "Yes" if row.get("likely_already_fixed") else "No"
    replied = "Yes" if row.get("maintainer_replied") else "No"
    title = (row.get("title") or "").replace("|", "\\|")
    staleness = row["staleness_days"] if row["staleness_days"] is not None else "?"
    return (
        f"| [#{row['number']}]({row.get('html_url', '')}) | {title} | {row['status']} "
        f"| {row['fit_tag']} | {staleness} | {replied} | {fixed} |"
    )


def _sweep_report_path(base_path: str | Path, repo_name: str) -> Path:
    """<base>.md -> <base>_<owner>_<repo>.md, so sweeping multiple repos in
    separate calls never overwrites an earlier repo's report."""
    base = Path(base_path)
    safe_repo = repo_name.replace("/", "_")
    return base.with_name(f"{base.stem}_{safe_repo}{base.suffix}")


def _write_sweep_report(
    repo_name: str,
    main_rows: list[dict],
    old_rows: list[dict],
    path: str | Path,
    my_rows: list[dict] | None = None,
) -> None:
    my_rows = my_rows or []
    lines = [
        "# issue-radar sweep report",
        "",
        "Pull requests are excluded from every count here (the GitHub REST "
        "`/issues` endpoint mixes PRs into its results; we filter them out via "
        "the `pull_request` field). A repo's `open_issues_count` on GitHub "
        "bundles open issues *and* open PRs together, so it reads higher than "
        "the counts below - that's expected, not a bug.",
        "",
        f"## {repo_name} ({len(main_rows) + len(old_rows) + len(my_rows)} issues)",
        "",
        _sweep_table_header(),
    ]
    for row in main_rows:
        lines.append(_sweep_table_row(row))
    lines.append("")

    if old_rows:
        lines.append(f"### Old / unanswered ({len(old_rows)} issues)")
        lines.append("")
        lines.append(_sweep_table_header())
        for row in old_rows:
            lines.append(_sweep_table_row(row))
        lines.append("")

    if my_rows:
        lines.append(f"### My issues ({len(my_rows)} issues)")
        lines.append("")
        lines.append(_sweep_table_header())
        for row in my_rows:
            lines.append(_sweep_table_row(row))
        lines.append("")

    Path(path).write_text("\n".join(lines))


def run_sweep(
    config: Config,
    dry_run: bool,
    token: str | None,
    repo_names: list[str] | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    client = GitHubClient(
        token=token,
        max_total_backoff_seconds=config.limits.max_total_backoff_seconds,
        max_requests=config.limits.max_api_calls_per_run,
    )

    repos_to_sweep = [r for r in config.repos if repo_names is None or r.name in repo_names]
    if repo_names:
        known = {r.name for r in config.repos}
        for name in repo_names:
            if name not in known:
                print(f"warning: --repo {name} is not in config.yaml, ignoring", file=sys.stderr)

    had_error = False
    sections: dict[str, dict[str, list[dict]]] = {}
    total_issues_scanned = 0

    for repo_cfg in repos_to_sweep:
        positive_keywords, negative_keywords, claim_phrases, reserved_labels = effective_keywords(repo_cfg, config)
        rows: list[dict] = []
        my_rows: list[dict] = []

        try:
            issues = list(client.list_issues(repo_cfg.name, since=None, state="open"))
        except (BackoffBudgetExceeded, RequestBudgetExceeded) as e:
            print(f"warning: run budget exceeded, stopping before {repo_cfg.name}: {e}", file=sys.stderr)
            had_error = True
            break
        except GitHubAPIError as e:
            print(f"warning: skipping {repo_cfg.name} after fetch error: {e}", file=sys.stderr)
            had_error = True
            continue

        budget_exhausted = False
        for issue in issues:
            number = issue["number"]
            total_issues_scanned += 1
            try:
                comments = list(client.list_issue_comments(repo_cfg.name, number))
                timeline = list(client.list_issue_timeline(repo_cfg.name, number))
                timeline += _resolve_comment_mentioned_prs(client, repo_cfg.name, number, comments)
            except (BackoffBudgetExceeded, RequestBudgetExceeded) as e:
                print(
                    f"warning: run budget exceeded while sweeping {repo_cfg.name}#{number}, stopping: {e}",
                    file=sys.stderr,
                )
                had_error = True
                budget_exhausted = True
                break
            except GitHubAPIError as e:
                print(f"warning: skipping {repo_cfg.name}#{number} after error: {e}", file=sys.stderr)
                had_error = True
                continue

            result = triage_issue(
                issue=issue,
                comments=comments,
                timeline=timeline,
                repo_name=repo_cfg.name,
                positive_keywords=positive_keywords,
                negative_keywords=negative_keywords,
                claim_phrases=claim_phrases,
                reserved_labels=reserved_labels,
                now=now,
            )

            result["html_url"] = issue.get("html_url")
            result["maintainer_replied"] = maintainer_replied(comments, repo_cfg.reviewers)

            if _is_my_issue(issue, config.my_username):
                # Your own issues get their own section regardless of
                # status - this is a personal tracking view, not subject
                # to the free/claimed logic that governs other people's.
                my_rows.append(result)
                continue

            if result["status"] not in SWEEP_KEPT_STATUSES:
                continue  # skip PRs (already excluded by list_issues) and CLAIMED/HAS-PR

            rows.append(result)

        very_old = config.sweep.very_old_days_threshold
        main_rows = [r for r in rows if not _is_old_unanswered(r, very_old)]
        old_rows = [r for r in rows if _is_old_unanswered(r, very_old)]
        main_rows.sort(key=_main_sort_key)
        old_rows.sort(key=_old_unanswered_sort_key)
        my_rows.sort(key=_main_sort_key)
        sections[repo_cfg.name] = {"main": main_rows, "old_unanswered": old_rows, "my_issues": my_rows}

        # Written per-repo (not batched at the end) so sweeping multiple
        # repos in separate calls never overwrites an earlier repo's file.
        report_path = _sweep_report_path(config.sweep_report_file, repo_cfg.name)
        _write_sweep_report(repo_cfg.name, main_rows, old_rows, report_path, my_rows=my_rows)
        print(f"Sweep report for {repo_cfg.name} written to {report_path}", file=sys.stderr)

        if budget_exhausted:
            break

    print(
        f"Open issues scanned (pull requests excluded): {total_issues_scanned}",
        file=sys.stderr,
    )
    print(f"API requests used this run: {client.request_count}", file=sys.stderr)

    if dry_run:
        for repo_name, repo_sections in sections.items():
            main_rows, old_rows = repo_sections["main"], repo_sections["old_unanswered"]
            print(f"\n=== {repo_name} ({len(main_rows)} main + {len(old_rows)} old/unanswered) ===")
            for row in main_rows[:15]:
                print(
                    f"#{row['number']:<6} {row['status']:<12} {row['fit_tag']:<6} "
                    f"replied={row.get('maintainer_replied')!s:<5} staleness={row['staleness_days']}d  {row['title']}"
                )

    return 1 if had_error else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)

    token, token_source = resolve_github_token()
    if token:
        print(f"using GitHub token from {token_source}", file=sys.stderr)
    else:
        print(
            "warning: no GitHub token available (gh CLI, GITHUB_TOKEN, GH_PAT); "
            "unauthenticated requests have a low rate limit",
            file=sys.stderr,
        )

    config = load_config(args.config)
    dry_run = args.dry_run or config.notifier.dry_run

    if args.mode == "poll":
        return run_poll(config, config.state_file, dry_run, token)
    elif args.mode == "sweep":
        return run_sweep(config, dry_run, token, getattr(args, "repo", None))

    return 1


if __name__ == "__main__":
    sys.exit(main())
