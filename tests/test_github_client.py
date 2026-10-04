from unittest.mock import patch

import pytest

from radar.github_client import (
    BackoffBudgetExceeded,
    GitHubAPIError,
    GitHubClient,
    RequestBudgetExceeded,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, links=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else []
        self.headers = headers or {}
        self.links = links or {}
        self.text = text

    def json(self):
        return self._json_data


class FakeSession:
    """Returns one canned FakeResponse per call to .request(), in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


def make_client(responses):
    session = FakeSession(responses)
    client = GitHubClient(token="fake-token", session=session)
    return client, session


def test_list_issues_filters_out_pull_requests():
    issue = {"number": 1, "title": "a real issue"}
    pr_as_issue = {"number": 2, "title": "a pr", "pull_request": {"url": "https://api.github.com/x"}}
    client, session = make_client([FakeResponse(json_data=[issue, pr_as_issue])])

    results = list(client.list_issues("owner/repo"))

    assert results == [issue]


def test_list_issues_sends_since_and_state():
    client, session = make_client([FakeResponse(json_data=[])])

    list(client.list_issues("owner/repo", since="2026-01-01T00:00:00Z", state="open"))

    params = session.calls[0]["params"]
    assert params["since"] == "2026-01-01T00:00:00Z"
    assert params["state"] == "open"


def test_pagination_follows_link_header():
    page1 = FakeResponse(
        json_data=[{"number": 1}],
        links={"next": {"url": "https://api.github.com/repos/owner/repo/issues?page=2"}},
    )
    page2 = FakeResponse(json_data=[{"number": 2}], links={})
    client, session = make_client([page1, page2])

    results = list(client.list_issues("owner/repo"))

    assert [r["number"] for r in results] == [1, 2]
    assert len(session.calls) == 2
    assert session.calls[1]["url"] == "https://api.github.com/repos/owner/repo/issues?page=2"


def test_primary_rate_limit_waits_until_reset():
    rate_limited = FakeResponse(
        status_code=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"},
    )
    ok = FakeResponse(json_data=[])
    client, session = make_client([rate_limited, ok])

    with patch("radar.github_client.time.time", return_value=970), patch("radar.github_client.time.sleep") as sleep:
        list(client.list_issues("owner/repo"))

    sleep.assert_called_once_with(30.0)
    assert len(session.calls) == 2


def test_secondary_rate_limit_uses_retry_after():
    rate_limited = FakeResponse(status_code=429, headers={"Retry-After": "5"})
    ok = FakeResponse(json_data=[])
    client, session = make_client([rate_limited, ok])

    with patch("radar.github_client.time.sleep") as sleep:
        list(client.list_issues("owner/repo"))

    sleep.assert_called_once_with(5.0)


def test_persistent_server_error_raises_after_retries():
    responses = [FakeResponse(status_code=500) for _ in range(10)]
    client, session = make_client(responses)

    with patch("radar.github_client.time.sleep"):
        with pytest.raises(GitHubAPIError) as exc_info:
            list(client.list_issues("owner/repo"))

    assert exc_info.value.status_code == 500


def test_non_200_non_retryable_status_raises_immediately():
    client, session = make_client([FakeResponse(status_code=404, text="Not Found")])

    with pytest.raises(GitHubAPIError) as exc_info:
        list(client.list_issues("owner/repo"))

    assert exc_info.value.status_code == 404
    assert len(session.calls) == 1


def test_list_issue_comments_builds_correct_url():
    client, session = make_client([FakeResponse(json_data=[{"id": 1, "body": "assign me"}])])

    results = list(client.list_issue_comments("owner/repo", 42))

    assert results == [{"id": 1, "body": "assign me"}]
    assert session.calls[0]["url"] == "https://api.github.com/repos/owner/repo/issues/42/comments"


def test_list_issue_timeline_builds_correct_url():
    client, session = make_client([FakeResponse(json_data=[{"event": "cross-referenced"}])])

    results = list(client.list_issue_timeline("owner/repo", 42))

    assert results == [{"event": "cross-referenced"}]
    assert session.calls[0]["url"] == "https://api.github.com/repos/owner/repo/issues/42/timeline"


def test_auth_header_included_when_token_set():
    client, session = make_client([FakeResponse(json_data=[])])

    list(client.list_issues("owner/repo"))

    assert session.calls[0]["headers"]["Authorization"] == "Bearer fake-token"


def test_no_auth_header_when_token_missing():
    session = FakeSession([FakeResponse(json_data=[])])
    client = GitHubClient(token=None, session=session)

    list(client.list_issues("owner/repo"))

    assert "Authorization" not in session.calls[0]["headers"]


def test_backoff_budget_exceeded_stops_sleeping():
    # Reset is far enough away that a single wait blows the whole budget.
    rate_limited = FakeResponse(
        status_code=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "10000"},
    )
    session = FakeSession([rate_limited])
    client = GitHubClient(token="t", session=session, max_total_backoff_seconds=60)

    with patch("radar.github_client.time.time", return_value=0), patch("radar.github_client.time.sleep") as sleep:
        with pytest.raises(BackoffBudgetExceeded):
            list(client.list_issues("owner/repo"))

    sleep.assert_not_called()


def test_backoff_budget_allows_waits_under_cap():
    rate_limited = FakeResponse(status_code=429, headers={"Retry-After": "5"})
    ok = FakeResponse(json_data=[{"number": 1}])
    session = FakeSession([rate_limited, ok])
    client = GitHubClient(token="t", session=session, max_total_backoff_seconds=60)

    with patch("radar.github_client.time.sleep") as sleep:
        results = list(client.list_issues("owner/repo"))

    sleep.assert_called_once_with(5.0)
    assert results == [{"number": 1}]


def test_request_budget_exceeded_before_sending():
    session = FakeSession([FakeResponse(json_data=[{"number": 1}])])
    client = GitHubClient(token="t", session=session, max_requests=0)

    with pytest.raises(RequestBudgetExceeded):
        list(client.list_issues("owner/repo"))

    assert len(session.calls) == 0


def test_request_budget_allows_exactly_the_cap():
    page1 = FakeResponse(
        json_data=[{"number": 1}],
        links={"next": {"url": "https://api.github.com/repos/owner/repo/issues?page=2"}},
    )
    page2 = FakeResponse(json_data=[{"number": 2}])
    session = FakeSession([page1, page2])
    client = GitHubClient(token="t", session=session, max_requests=2)

    results = list(client.list_issues("owner/repo"))

    assert [r["number"] for r in results] == [1, 2]
    assert client.request_count == 2


def test_get_issue_returns_parsed_json():
    client, session = make_client([FakeResponse(json_data={"number": 42, "title": "x"})])

    result = client.get_issue("owner/repo", 42)

    assert result == {"number": 42, "title": "x"}
    assert session.calls[0]["url"] == "https://api.github.com/repos/owner/repo/issues/42"


def test_get_issue_raises_on_404():
    client, session = make_client([FakeResponse(status_code=404, text="Not Found")])

    with pytest.raises(GitHubAPIError) as exc_info:
        client.get_issue("owner/repo", 999999)

    assert exc_info.value.status_code == 404


def test_get_issue_pull_request_sidecar_passed_through():
    pr_json = {
        "number": 1259,
        "state": "open",
        "pull_request": {"url": "...", "html_url": "...", "merged_at": None},
    }
    client, session = make_client([FakeResponse(json_data=pr_json)])

    result = client.get_issue("owner/repo", 1259)

    assert "pull_request" in result
