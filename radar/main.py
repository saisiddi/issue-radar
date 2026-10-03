from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from radar.config import load_config
from radar.state import load_state, save_state

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


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


def run_poll(config, state_path: str, dry_run: bool) -> int:
    raise NotImplementedError("implemented in step 2")


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
        return run_poll(config, config.state_file, dry_run)
    elif args.mode == "sweep":
        return run_sweep(config, dry_run)

    return 1


if __name__ == "__main__":
    sys.exit(main())
