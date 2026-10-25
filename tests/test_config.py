from nexus.config import Settings


def test_defaults_are_local_and_safe(monkeypatch):
    monkeypatch.delenv("NEXUS_HOST", raising=False)
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1"
    assert s.auto_approve_outbound is False
    assert s.fetch_block_private is True
    assert s.db_path.name == "nexus.db"


def test_provider_keys_use_standard_env_names(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("NEXUS_PORT", "9000")
    s = Settings(_env_file=None)
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert s.port == 9000
