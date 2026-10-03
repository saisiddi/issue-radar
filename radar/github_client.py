from __future__ import annotations

import time
from typing import Iterator

import requests

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 900


class GitHubAPIError(Exception):
    """Raised when a request fails after exhausting retries.

    Callers should catch this per-repo so one repo's failure doesn't abort
    the whole poll/sweep run.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str = API_BASE, session: requests.Session | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

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
            response = self.session.request(method, url, headers=self._headers(), params=params, timeout=30)

            if response.status_code in (403, 429) and self._is_rate_limited(response):
                time.sleep(self._wait_seconds(response, attempt))
                continue

            if response.status_code >= 500:
                time.sleep(min(2**attempt, MAX_BACKOFF_SECONDS))
                continue

            return response

        raise GitHubAPIError(
            f"Exhausted retries for {method} {url} (last status {response.status_code})",
            response.status_code,
        )

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
