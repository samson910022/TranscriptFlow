import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_example_config_loads_without_private_defaults(monkeypatch):
    import config_loader

    monkeypatch.setattr(config_loader, "CONFIG_PATH", SCRIPTS / "config.example.json")
    config_loader._config = None
    cfg = config_loader.get_config()

    assert cfg["api"].get("api_key", "") == ""
    assert "turn_key-solution" not in str(cfg)
    assert "/Users/" not in str(cfg)


def test_environment_overrides_api_key(monkeypatch):
    import config_loader

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_loader._config = None

    assert config_loader.get_api_config()["api_key"] == "test-key"


def test_environment_overrides_base_url(monkeypatch):
    import config_loader

    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test")
    config_loader._config = None

    cfg = config_loader.get_api_config()
    assert cfg["base_url"] == "https://example.test"
    assert cfg["proxy_url"] == "https://example.test"


def test_legacy_litellm_environment_still_supported(monkeypatch):
    import config_loader

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("LITELLM_PROXY_KEY", "legacy-key")
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://localhost:4000")
    config_loader._config = None

    cfg = config_loader.get_api_config()
    assert cfg["api_key"] == "legacy-key"
    assert cfg["base_url"] == "http://localhost:4000"
