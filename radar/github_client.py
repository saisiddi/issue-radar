from __future__ import annotations

import time
from typing import Iterator

import requests

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 900
DEFAULT_MAX_TOTAL_BACKOFF_SECONDS = 300  # cap per-run sleep; a long wait just burns Actions minutes


class GitHubAPIError(Exception):
    """Raised when a request fails after exhausting retries.

    Callers should catch this per-repo so one repo's failure doesn't abort
    the whole poll/sweep run.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BackoffBudgetExceeded(GitHubAPIError):
    """Cumulative rate-limit sleep for this run exceeded its budget."""


class RequestBudgetExceeded(GitHubAPIError):
    """This run issued more HTTP requests than its configured cap."""


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str = API_BASE,
        session: requests.Session | None = None,
        max_total_backoff_seconds: float = DEFAULT_MAX_TOTAL_BACKOFF_SECONDS,
        max_requests: int | None = None,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.max_total_backoff_seconds = max_total_backoff_seconds
        self.max_requests = max_requests
        self.total_backoff_seconds = 0.0
        self.request_count = 0

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, url: str, params: dict | None = None) -> requests.Response:
        response = None
        for attempt in range(MAX_ATTEMPTS):
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise RequestBudgetExceeded(
                    f"Hit the {self.max_requests}-request budget for this run before {method} {url}"
                )
            self.request_count += 1
            response = self.session.request(method, url, headers=self._headers(), params=params, timeout=30)

            if response.status_code in (403, 429) and self._is_rate_limited(response):
                self._sleep_within_budget(self._wait_seconds(response, attempt))
                continue

            if response.status_code >= 500:
                self._sleep_within_budget(min(2**attempt, MAX_BACKOFF_SECONDS))
                continue

            return response

        raise GitHubAPIError(
            f"Exhausted retries for {method} {url} (last status {response.status_code})",
            response.status_code,
        )

    def _sleep_within_budget(self, seconds: float) -> None:
        self.total_backoff_seconds += seconds
        if self.total_backoff_seconds > self.max_total_backoff_seconds:
            raise BackoffBudgetExceeded(
                f"Cumulative backoff ({self.total_backoff_seconds:.0f}s) exceeded the "
                f"{self.max_total_backoff_seconds:.0f}s budget for this run"
            )
        time.sleep(seconds)

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        # Per GitHub docs: a 403/429 is a rate limit (primary or secondary) when
        # either Retry-After is set or the primary limit is exhausted.
        return bool(response.headers.get("Retry-After")) or response.headers.get("X-RateLimit-Remaining") == "0"

    @staticmethod
    def _wait_seconds(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            return float(retry_after)

        reset = response.headers.get("X-RateLimit-Reset")
        if response.headers.get("X-RateLimit-Remaining") == "0" and reset:
            return max(float(reset) - time.time(), 1.0)

        # Secondary rate limit with neither header: GitHub recommends waiting
        # at least a minute, then backing off exponentially.
        return min(60 * (2**attempt), MAX_BACKOFF_SECONDS)

    def _paginate(self, url: str, params: dict | None = None) -> Iterator[dict]:
        next_url: str | None = url
        next_params = dict(params or {})
        next_params.setdefault("per_page", PER_PAGE)

        while next_url:
            response = self._request("GET", next_url, params=next_params)
            if response.status_code != 200:
                raise GitHubAPIError(
                    f"GET {next_url} failed with {response.status_code}: {response.text[:200]}",
                    response.status_code,
                )
            for item in response.json():
                yield item

            next_url = response.links.get("next", {}).get("url")
            next_params = None  # already encoded into next_url by GitHub

    def list_issues(self, repo: str, since: str | None = None, state: str = "open") -> Iterator[dict]:
        """Yield issues for repo, excluding pull requests.

        The /issues endpoint returns PRs alongside issues; a PR's payload
        always has a `pull_request` key, which we filter out here.
        """
        params = {"state": state, "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        for item in self._paginate(f"{self.base_url}/repos/{repo}/issues", params=params):
            if "pull_request" in item:
                continue
            yield item

    def list_issue_comments(self, repo: str, issue_number: int) -> Iterator[dict]:
        yield from self._paginate(f"{self.base_url}/repos/{repo}/issues/{issue_number}/comments")

    def list_issue_timeline(self, repo: str, issue_number: int) -> Iterator[dict]:
        yield from self._paginate(f"{self.base_url}/repos/{repo}/issues/{issue_number}/timeline")

    def get_issue(self, repo: str, number: int) -> dict:
        """Fetch a single issue or PR by number.

        If `number` is actually a PR, the response includes a
        `pull_request` sidecar object (url/html_url/diff_url/patch_url/
        merged_at) - same shape as a timeline cross-referenced event's
        source.issue. Used to resolve a bare "#N" mentioned in a comment,
        which GitHub's own cross-reference system doesn't surface on this
        issue's own timeline (see triage.extract_comment_pr_mentions).
        """
        response = self._request("GET", f"{self.base_url}/repos/{repo}/issues/{number}")
        if response.status_code != 200:
            raise GitHubAPIError(
                f"GET issue {repo}#{number} failed with {response.status_code}: {response.text[:200]}",
                response.status_code,
            )
        return response.json()
