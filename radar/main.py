from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar.config import Config, load_config
from radar.github_client import (
    BackoffBudgetExceeded,
    GitHubAPIError,
    GitHubClient,
    RequestBudgetExceeded,
)
from radar.notify import NotifierConfigError, NotifierError, format_alert, get_notifier
from radar.state import load_state, save_state
from radar.triage import triage_issue

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
DEFAULT_DOTENV_PATH = Path(__file__).parent.parent / ".env"
ALERTABLE_STATUSES = {"OPEN-FREE", "CONTESTED", "UNSURE", "DISCUSS-ONLY"}
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


def _summary_reason(result: dict) -> str:
    if result["status"] == "UNSURE":
        return "; ".join(result.get("linked_pr_notes") or []) or "uncertain signal, check manually"
    if result["status"] == "CONTESTED":
        return f"{result['claim_comments']} claim comments, unassigned"
    if result["status"] == "DISCUSS-ONLY":
        return f"reserved label: {', '.join(result.get('reserved_labels') or [])}"
    abandoned = [n for n in (result.get("linked_pr_notes") or []) if "closed unmerged" in n]
    if abandoned:
        return "; ".join(abandoned)
    return f"fit {result['fit_tag']} (matched: {', '.join(result.get('matched_keywords') or []) or 'none'})"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    common.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them")

    parser = argparse.ArgumentParser(
        prog="radar", description="GitHub issue alert and triage tool", parents=[common]
    )

    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("poll", help="Alert on new/changed issues since last run", parents=[common])
    subparsers.add_parser("sweep", help="One-time report over all open issues", parents=[common])

    return parser


def run_poll(
    config: Config, state_path: str, dry_run: bool, token: str | None, now: datetime | None = None
) -> int:
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

            try:
                comments = list(client.list_issue_comments(repo_cfg.name, number))
                timeline = list(client.list_issue_timeline(repo_cfg.name, number))
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
                positive_keywords=config.positive_keywords,
                negative_keywords=config.negative_keywords,
                claim_phrases=config.claim_phrases,
                reserved_labels=config.reserved_labels,
            )

            if result["status"] not in ALERTABLE_STATUSES:
                continue

            message = format_alert(result, issue, repo_cfg)
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


def run_sweep(config, dry_run: bool) -> int:
    raise NotImplementedError("implemented in step 5")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")
    if not token:
        print("warning: no GITHUB_TOKEN or GH_PAT set; unauthenticated requests have a low rate limit", file=sys.stderr)

    config = load_config(args.config)
    dry_run = args.dry_run or config.notifier.dry_run

    if args.mode == "poll":
        return run_poll(config, config.state_file, dry_run, token)
    elif args.mode == "sweep":
        return run_sweep(config, dry_run)

    return 1


if __name__ == "__main__":
    sys.exit(main())
