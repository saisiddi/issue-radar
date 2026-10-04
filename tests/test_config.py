from pathlib import Path

from radar.config import (
    Config,
    LimitsConfig,
    LLMConfig,
    NotifierConfig,
    PollConfig,
    RepoConfig,
    SweepConfig,
    effective_keywords,
    load_config,
)

CONFIG_PATH = Path(__file__).parent.parent / "radar" / "config.yaml"

MINIMAL_CONFIG_WITH_OVERRIDE = """
repos:
  - name: owner/plain-repo
    org: Org
  - name: owner/override-repo
    org: Org
    positive_keywords: [Rust, Go]
    negative_keywords: [PHP]
    claim_phrases: ["I call dibs"]
    reserved_labels: [hackathon-only]

skills:
  positive_keywords: [python]
  negative_keywords: [react]

claim_phrases:
  - "assign me"

reserved_labels:
  - gsoc-idea
"""


def test_load_config_repos():
    config = load_config(CONFIG_PATH)
    names = [r.name for r in config.repos]
    assert "OWASP/Nettacker" in names
    assert "GreedyBear-Project/GreedyBear" in names
    assert "intelowlproject/IntelOwl" in names
    assert "openwisp/openwisp-firmware-upgrader" in names


def test_load_config_reserve_flag():
    config = load_config(CONFIG_PATH)
    firmware = next(r for r in config.repos if r.name == "openwisp/openwisp-firmware-upgrader")
    assert firmware.reserve is True
    assert firmware.requires_assignment is False

    nettacker = next(r for r in config.repos if r.name == "OWASP/Nettacker")
    assert nettacker.requires_assignment is True


def test_load_config_keywords_lowercased():
    config = load_config(CONFIG_PATH)
    assert "python" in config.positive_keywords
    assert "react" in config.negative_keywords


def test_load_config_defaults():
    config = load_config(CONFIG_PATH)
    assert config.notifier.type == "telegram"
    assert config.llm.enabled is False


def test_load_config_limits():
    config = load_config(CONFIG_PATH)
    assert config.limits.max_total_backoff_seconds == 300
    assert config.limits.max_api_calls_per_run is None


def test_load_config_poll_bootstrap_window():
    config = load_config(CONFIG_PATH)
    assert config.poll.first_run_window_days == 3


def test_load_config_sweep_very_old_threshold():
    config = load_config(CONFIG_PATH)
    assert config.sweep.very_old_days_threshold == 180


def test_load_config_my_username():
    config = load_config(CONFIG_PATH)
    assert config.my_username == "saisiddi"


def test_load_config_my_username_defaults_to_none(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG_WITH_OVERRIDE)
    config = load_config(path)
    assert config.my_username is None


class TestPerRepoOverrides:
    def test_repo_without_overrides_has_none_fields(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(MINIMAL_CONFIG_WITH_OVERRIDE)
        config = load_config(path)

        plain = next(r for r in config.repos if r.name == "owner/plain-repo")
        assert plain.positive_keywords is None
        assert plain.negative_keywords is None
        assert plain.claim_phrases is None
        assert plain.reserved_labels is None

    def test_repo_with_overrides_parses_and_lowercases(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(MINIMAL_CONFIG_WITH_OVERRIDE)
        config = load_config(path)

        override = next(r for r in config.repos if r.name == "owner/override-repo")
        assert override.positive_keywords == ["rust", "go"]
        assert override.negative_keywords == ["php"]
        assert override.claim_phrases == ["i call dibs"]
        assert override.reserved_labels == ["hackathon-only"]

    def test_effective_keywords_falls_back_to_global_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(MINIMAL_CONFIG_WITH_OVERRIDE)
        config = load_config(path)

        plain = next(r for r in config.repos if r.name == "owner/plain-repo")
        positive, negative, claims, reserved = effective_keywords(plain, config)

        assert positive == config.positive_keywords == ["python"]
        assert negative == config.negative_keywords == ["react"]
        assert claims == config.claim_phrases == ["assign me"]
        assert reserved == config.reserved_labels == ["gsoc-idea"]

    def test_effective_keywords_uses_repo_override_instead_of_global(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(MINIMAL_CONFIG_WITH_OVERRIDE)
        config = load_config(path)

        override = next(r for r in config.repos if r.name == "owner/override-repo")
        positive, negative, claims, reserved = effective_keywords(override, config)

        assert positive == ["rust", "go"]
        assert negative == ["php"]
        assert claims == ["i call dibs"]
        assert reserved == ["hackathon-only"]

    def test_empty_override_list_is_respected_not_treated_as_missing(self):
        # An explicit [] must override the global list entirely, not fall
        # back to it - only None (field omitted) means "use the default".
        repo = RepoConfig(name="x/y", org="Org", positive_keywords=[])
        config = Config(
            repos=[repo],
            positive_keywords=["python"],
            negative_keywords=["react"],
            claim_phrases=["assign me"],
            reserved_labels=["gsoc-idea"],
            staleness_days_threshold=30,
            notifier=NotifierConfig(),
            llm=LLMConfig(),
            limits=LimitsConfig(),
            poll=PollConfig(),
            sweep=SweepConfig(),
            state_file="state.json",
            sweep_report_file="sweep_report.md",
        )
        positive, *_ = effective_keywords(repo, config)
        assert positive == []
