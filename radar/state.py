from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json


@dataclass
class RepoState:
    last_seen: str | None = None
    alerted_issue_numbers: list[int] = field(default_factory=list)


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)

    def for_repo(self, repo_name: str) -> RepoState:
        if repo_name not in self.repos:
            self.repos[repo_name] = RepoState()
        return self.repos[repo_name]

    def to_dict(self) -> dict:
        return {
            name: {
                "last_seen": rs.last_seen,
                "alerted_issue_numbers": rs.alerted_issue_numbers,
            }
            for name, rs in self.repos.items()
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "State":
        repos = {
            name: RepoState(
                last_seen=data.get("last_seen"),
                alerted_issue_numbers=list(data.get("alerted_issue_numbers", [])),
            )
            for name, data in raw.items()
        }
        return cls(repos=repos)


def load_state(path: str | Path) -> State:
    p = Path(path)
    if not p.exists():
        return State()
    with open(p, "r") as f:
        raw = json.load(f)
    return State.from_dict(raw)


def save_state(state: State, path: str | Path) -> None:
    with open(path, "w") as f:
        json.dump(state.to_dict(), f, indent=2, sort_keys=True)
        f.write("\n")
