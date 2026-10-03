import json

from radar.state import State, load_state, save_state


def test_new_state_empty():
    state = State()
    assert state.to_dict() == {}


def test_for_repo_creates_entry():
    state = State()
    repo_state = state.for_repo("OWASP/Nettacker")
    repo_state.last_seen = "2026-01-01T00:00:00Z"
    repo_state.alerted_issue_numbers.append(42)

    assert state.for_repo("OWASP/Nettacker").last_seen == "2026-01-01T00:00:00Z"
    assert state.for_repo("OWASP/Nettacker").alerted_issue_numbers == [42]


def test_round_trip_dict():
    state = State()
    state.for_repo("a/b").last_seen = "2026-01-01T00:00:00Z"
    state.for_repo("a/b").alerted_issue_numbers = [1, 2, 3]

    restored = State.from_dict(state.to_dict())
    assert restored.for_repo("a/b").last_seen == "2026-01-01T00:00:00Z"
    assert restored.for_repo("a/b").alerted_issue_numbers == [1, 2, 3]


def test_save_and_load_state(tmp_path):
    path = tmp_path / "state.json"
    state = State()
    state.for_repo("a/b").last_seen = "2026-01-01T00:00:00Z"
    state.for_repo("a/b").alerted_issue_numbers = [1, 2]
    save_state(state, path)

    on_disk = json.loads(path.read_text())
    assert on_disk["a/b"]["last_seen"] == "2026-01-01T00:00:00Z"

    loaded = load_state(path)
    assert loaded.for_repo("a/b").last_seen == "2026-01-01T00:00:00Z"
    assert loaded.for_repo("a/b").alerted_issue_numbers == [1, 2]


def test_load_state_missing_file_returns_empty(tmp_path):
    path = tmp_path / "does_not_exist.json"
    state = load_state(path)
    assert state.to_dict() == {}
