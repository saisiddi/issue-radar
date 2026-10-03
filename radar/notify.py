from __future__ import annotations

"""Pluggable alert notifiers. Implemented in step 4."""


class Notifier:
    def send(self, message: str) -> None:
        raise NotImplementedError("implemented in step 4")


class DryRunNotifier(Notifier):
    def send(self, message: str) -> None:
        print(message)


def get_notifier(config, dry_run: bool) -> Notifier:
    if dry_run:
        return DryRunNotifier()
    raise NotImplementedError("implemented in step 4")
