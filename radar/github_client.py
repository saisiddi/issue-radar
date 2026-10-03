from __future__ import annotations

"""GitHub REST API client. Implemented in step 2.

Read-only: this module must never call any endpoint that creates, assigns,
labels, or comments on an issue or PR.
"""


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token

    def list_issues(self, repo: str, since: str | None = None):
        raise NotImplementedError("implemented in step 2")

    def list_issue_comments(self, repo: str, issue_number: int):
        raise NotImplementedError("implemented in step 2")

    def list_issue_timeline(self, repo: str, issue_number: int):
        raise NotImplementedError("implemented in step 2")
