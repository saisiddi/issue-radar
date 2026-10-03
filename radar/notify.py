from __future__ import annotations

import os

import requests

from radar.config import RepoConfig
from radar.triage import days_since

FOOTER = "Read the issue and the code before commenting. Ask for assignment with a specific plan."

TELEGRAM_MAX_LENGTH = 4096
DISCORD_MAX_LENGTH = 2000


class NotifierError(Exception):
    pass


class NotifierConfigError(NotifierError):
    pass


def _body_excerpt(body: str | None, max_lines: int = 3, max_line_len: int = 200) -> str:
    if not body:
        return "(no description)"
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "\n".join(line[:max_line_len] for line in lines[:max_lines]) or "(no description)"


def _label_names(labels: list) -> list[str]:
    return [label.get("name") if isinstance(label, dict) else str(label) for label in labels]


def format_alert(triage: dict, issue: dict, repo_cfg: RepoConfig) -> str:
    labels = ", ".join(_label_names(issue.get("labels", []))) or "none"
    author = (issue.get("user") or {}).get("login", "unknown")
    age_days = days_since(issue.get("created_at"))
    reviewers = ", ".join(repo_cfg.reviewers) if repo_cfg.reviewers else "none listed"
    matched = ", ".join(triage.get("matched_keywords") or []) or "none"

    lines = [
        f"[{repo_cfg.name}] (org: {repo_cfg.org})",
        f"#{triage['number']}: {triage.get('title') or ''}",
        issue.get("html_url", ""),
        f"Labels: {labels}",
        f"Author: {author}  Age: {age_days}d",
        f"Status: {triage['status']}   Fit: {triage['fit_tag']} (matched: {matched})",
        f"Likely reviewers: {reviewers}",
        "",
        _body_excerpt(issue.get("body")),
    ]

    # UNSURE must always say why, so it can be dismissed or chased in seconds.
    if triage["status"] == "UNSURE":
        reason = "; ".join(triage.get("linked_pr_notes") or []) or "uncertain signal, check manually"
        lines += ["", f"Why UNSURE: {reason}"]

    if triage["status"] == "DISCUSS-ONLY" and triage.get("reserved_labels"):
        lines += ["", f"Reserved label: {', '.join(triage['reserved_labels'])}"]

    if triage["status"] == "CONTESTED":
        lines += ["", f"Claim comments: {triage['claim_comments']}"]

    # A closed-unmerged previous attempt is worth surfacing even when it
    # didn't drive the status (e.g. OPEN-FREE after an abandoned PR).
    abandoned_notes = [n for n in (triage.get("linked_pr_notes") or []) if "closed unmerged" in n]
    if abandoned_notes and triage["status"] != "UNSURE":
        lines += ["", "Note: " + "; ".join(abandoned_notes)]

    lines += ["", FOOTER]
    return "\n".join(lines)


class Notifier:
    def send(self, message: str) -> None:
        raise NotImplementedError


class DryRunNotifier(Notifier):
    def send(self, message: str) -> None:
        print(message)
        print("-" * 60)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, session: requests.Session | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message[:TELEGRAM_MAX_LENGTH],
            "disable_web_page_preview": True,
        }
        response = self.session.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            raise NotifierError(f"Telegram send failed with {response.status_code}: {response.text[:200]}")


class DiscordNotifier(Notifier):
    def __init__(self, webhook_url: str, session: requests.Session | None = None):
        self.webhook_url = webhook_url
        self.session = session or requests.Session()

    def send(self, message: str) -> None:
        response = self.session.post(self.webhook_url, json={"content": message[:DISCORD_MAX_LENGTH]}, timeout=15)
        if response.status_code not in (200, 204):
            raise NotifierError(f"Discord send failed with {response.status_code}: {response.text[:200]}")


def get_notifier(notifier_type: str, dry_run: bool) -> Notifier:
    if dry_run:
        return DryRunNotifier()

    if notifier_type == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise NotifierConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set")
        return TelegramNotifier(token, chat_id)

    if notifier_type == "discord":
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook_url:
            raise NotifierConfigError("DISCORD_WEBHOOK_URL must be set")
        return DiscordNotifier(webhook_url)

    raise NotifierConfigError(f"Unknown notifier type: {notifier_type!r}")
