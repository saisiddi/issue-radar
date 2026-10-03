from pathlib import Path

from radar.config import load_config

CONFIG_PATH = Path(__file__).parent.parent / "radar" / "config.yaml"


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
