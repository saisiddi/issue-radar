from __future__ import annotations

import argparse
import os
import sys
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
ALERTABLE_STATUSES = {"OPEN-FREE", "CONTESTED", "UNSURE", "DISCUSS-ONLY"}


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


def run_poll(config: Config, state_path: str, dry_run: bool, token: str | None) -> int:
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
    for repo_cfg in config.repos:
        repo_state = state.for_repo(repo_cfg.name)
        try:
            issues = list(client.list_issues(repo_cfg.name, since=repo_state.last_seen, state="open"))
        except (BackoffBudgetExceeded, RequestBudgetExceeded) as e:
            print(f"warning: run budget exceeded, stopping before {repo_cfg.name}: {e}", file=sys.stderr)
            had_error = True
            break
        except GitHubAPIError as e:
            print(f"warning: skipping {repo_cfg.name} after fetch error: {e}", file=sys.stderr)
            had_error = True
            continue

        latest_seen = repo_state.last_seen
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

        repo_state.last_seen = latest_seen
        save_state(state, state_path)  # persist progressively so a crash doesn't lose earlier repos' progress

        if budget_exhausted:
            break

    return 1 if had_error else 0


def run_sweep(config, dry_run: bool) -> int:
    raise NotImplementedError("implemented in step 5")


def main(argv: list[str] | None = None) -> int:
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
