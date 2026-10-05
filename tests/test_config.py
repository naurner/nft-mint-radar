from pathlib import Path

from wlparser.config import Settings, load_env_file, load_settings


def test_defaults_are_sane():
    settings = Settings()
    assert settings.chain.poll_interval_s == 2.0
    assert settings.chain.surge_threshold == 25
    assert settings.alerts.min_score == 45
    assert settings.llm.filter_alerts is True
    assert settings.poster.max_posts_per_day is None
    assert settings.poster.max_quotes_per_day == 5
    assert settings.digest.enabled is True
    assert settings.digest.times == [
        "00:00", "02:00", "04:00", "06:00", "08:00", "10:00",
        "12:00", "14:00", "16:00", "18:00", "20:00", "22:00",
    ]


def test_load_from_yaml(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "chain:\n  rpc_url: https://rpc.example\n  name: robinhood\n"
        "alerts:\n  min_score: 70\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.chain.rpc_url == "https://rpc.example"
    assert settings.alerts.min_score == 70


def test_missing_file_returns_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml", tmp_path / ".env")
    assert settings.chain.rpc_url == ""
    assert settings.is_configured is False


def test_is_configured_true_with_rpc():
    assert Settings(chain={"rpc_url": "https://rpc.example"}).is_configured is True


def test_env_file_is_parsed(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        '# комментарий\nTELEGRAM_BOT_TOKEN="abc"\nTELEGRAM_CHAT_ID=42\n\n',
        encoding="utf-8",
    )
    values = load_env_file(env)
    assert values == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42"}


def test_secrets_come_from_env_file(tmp_path):
    (tmp_path / "config.yaml").write_text("chain:\n  rpc_url: x\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.telegram_chat_id == "42"


def test_empty_explorer_url_falls_back_to_default(tmp_path):
    # Мастер разрешает пропустить вопрос про эксплорер, но ссылки нужны всегда.
    (tmp_path / "config.yaml").write_text(
        'chain:\n  rpc_url: x\n  explorer_url: ""\n', encoding="utf-8"
    )
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.chain.explorer_url == "https://robinscan.io"


def test_explicit_explorer_url_is_kept(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "chain:\n  rpc_url: x\n  explorer_url: https://other.example\n", encoding="utf-8"
    )
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.chain.explorer_url == "https://other.example"


def test_twitter_is_off_by_default_with_ready_queries():
    settings = Settings()
    assert settings.twitter.enabled is False
    assert any("robinhood" in q.lower() for q in settings.twitter.queries)
    assert settings.twitter.watchlist == []


def test_twitter_queries_can_be_overridden(tmp_path):
    (tmp_path / "config.yaml").write_text(
        'chain:\n  rpc_url: x\ntwitter:\n  enabled: true\n  queries:\n    - "my query"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.twitter.enabled is True
    assert settings.twitter.queries == ["my query"]


def test_config_and_env_paths_come_from_environment(monkeypatch):
    """В контейнере конфиг и секреты лежат в томе, а не в рабочей папке.

    Значения читаются на импорте модуля — как раз тогда, когда Docker
    уже выставил переменные окружения.
    """
    import importlib

    import wlparser.config as config_module

    monkeypatch.setenv("WLPARSER_CONFIG", "/app/config/config.yaml")
    monkeypatch.setenv("WLPARSER_ENV", "/app/config/.env")
    reloaded = importlib.reload(config_module)
    try:
        assert str(reloaded.DEFAULT_CONFIG_PATH).endswith("config.yaml")
        assert "app" in str(reloaded.DEFAULT_CONFIG_PATH)
        assert "app" in str(reloaded.DEFAULT_ENV_PATH)
    finally:
        monkeypatch.delenv("WLPARSER_CONFIG")
        monkeypatch.delenv("WLPARSER_ENV")
        importlib.reload(config_module)


def test_config_paths_fall_back_to_project_layout():
    import importlib

    import wlparser.config as config_module

    reloaded = importlib.reload(config_module)
    assert reloaded.DEFAULT_CONFIG_PATH == Path("config/config.yaml")
    assert reloaded.DEFAULT_ENV_PATH == Path(".env")


def test_tracking_defaults_are_sane():
    settings = Settings()
    assert settings.tracking.enabled is True
    assert settings.tracking.refresh_interval_s == 300.0
    assert settings.tracking.max_duration_hours == 720.0
    assert settings.tracking.stale_after_checks == 12


def test_tracking_can_be_disabled(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "chain:\n  rpc_url: x\ntracking:\n  enabled: false\n", encoding="utf-8"
    )
    settings = load_settings(tmp_path / "config.yaml", tmp_path / ".env")
    assert settings.tracking.enabled is False
