from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RepoConfig:
    name: str
    org: str
    reviewers: list[str] = field(default_factory=list)
    requires_assignment: bool = True
    reserve: bool = False
    # None means "use the global default" (see effective_keywords); an
    # explicit list, even empty, overrides it entirely for this repo.
    positive_keywords: list[str] | None = None
    negative_keywords: list[str] | None = None
    claim_phrases: list[str] | None = None
    reserved_labels: list[str] | None = None


@dataclass
class NotifierConfig:
    type: str = "telegram"
    dry_run: bool = False


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str | None = None
    model: str | None = None


@dataclass
class LimitsConfig:
    max_total_backoff_seconds: float = 300.0
    max_api_calls_per_run: int | None = None


@dataclass
class PollConfig:
    # On a repo's very first poll (empty state), only look back this many
    # days instead of fetching every historical open issue - that's sweep's
    # job, not poll's.
    first_run_window_days: int = 3


@dataclass
class SweepConfig:
    # A kept issue with no maintainer-reviewer comment and no activity for
    # at least this many days goes in the report's separate "Old /
    # unanswered" section instead of the main table.
    very_old_days_threshold: int = 180


@dataclass
class Config:
    repos: list[RepoConfig]
    positive_keywords: list[str]
    negative_keywords: list[str]
    claim_phrases: list[str]
    reserved_labels: list[str]
    staleness_days_threshold: int
    notifier: NotifierConfig
    llm: LLMConfig
    limits: LimitsConfig
    poll: PollConfig
    sweep: SweepConfig
    state_file: str
    sweep_report_file: str
    # Your own GitHub login. Issues you authored are never alerted on in
    # poll, and are listed under a separate "My issues" section in sweep
    # reports instead of the main/old-unanswered tables.
    my_username: str | None = None


REPO_OVERRIDE_KEYWORD_LIST_FIELDS = ("positive_keywords", "negative_keywords", "claim_phrases", "reserved_labels")


def load_config(path: str | Path) -> Config:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    repos = []
    for repo_raw in raw["repos"]:
        repo_raw = dict(repo_raw)
        for key in REPO_OVERRIDE_KEYWORD_LIST_FIELDS:
            if repo_raw.get(key) is not None:
                repo_raw[key] = [v.lower() for v in repo_raw[key]]
        repos.append(RepoConfig(**repo_raw))

    skills = raw.get("skills", {})
    notifier_raw = raw.get("notifier", {})
    llm_raw = raw.get("llm", {})
    limits_raw = raw.get("limits", {})
    poll_raw = raw.get("poll", {})
    sweep_raw = raw.get("sweep", {})

    return Config(
        repos=repos,
        positive_keywords=[k.lower() for k in skills.get("positive_keywords", [])],
        negative_keywords=[k.lower() for k in skills.get("negative_keywords", [])],
        claim_phrases=[p.lower() for p in raw.get("claim_phrases", [])],
        reserved_labels=[l.lower() for l in raw.get("reserved_labels", [])],
        staleness_days_threshold=raw.get("staleness_days_threshold", 30),
        notifier=NotifierConfig(**notifier_raw),
        llm=LLMConfig(**llm_raw),
        limits=LimitsConfig(**limits_raw),
        poll=PollConfig(**poll_raw),
        sweep=SweepConfig(**sweep_raw),
        state_file=raw.get("state_file", "state.json"),
        sweep_report_file=raw.get("sweep_report_file", "sweep_report.md"),
        my_username=raw.get("my_username"),
    )


def effective_keywords(repo: RepoConfig, config: Config) -> tuple[list[str], list[str], list[str], list[str]]:
    """(positive_keywords, negative_keywords, claim_phrases, reserved_labels) for a repo.

    Falls back to the global config defaults for any field the repo doesn't
    override (None); an explicit list, even empty, overrides it entirely.
    """
    return (
        repo.positive_keywords if repo.positive_keywords is not None else config.positive_keywords,
        repo.negative_keywords if repo.negative_keywords is not None else config.negative_keywords,
        repo.claim_phrases if repo.claim_phrases is not None else config.claim_phrases,
        repo.reserved_labels if repo.reserved_labels is not None else config.reserved_labels,
    )
