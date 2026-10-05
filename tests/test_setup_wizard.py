import yaml

from wlparser.setup_wizard import (
    enable_poster,
    set_llm_model,
    use_local_db,
    write_config,
    write_env,
)


def test_write_config_creates_valid_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(
        path,
        rpc_url="https://rpc.example",
        chain_id=6975,
        explorer_url="https://explorer.example",
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["chain"]["rpc_url"] == "https://rpc.example"
    assert data["chain"]["chain_id"] == 6975
    assert data["chain"]["surge_threshold"] == 25


def test_write_config_preserves_existing_unrelated_settings(tmp_path):
    # Повторный запуск мастера не должен сбрасывать вручную выставленные пороги.
    path = tmp_path / "config.yaml"
    path.write_text("alerts:\n  min_score: 80\n", encoding="utf-8")
    write_config(path, rpc_url="https://rpc.example", chain_id=1, explorer_url="")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["alerts"]["min_score"] == 80
    assert data["chain"]["rpc_url"] == "https://rpc.example"


def test_write_env_writes_secrets(tmp_path):
    path = tmp_path / ".env"
    write_env(path, {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42"})
    content = path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=abc" in content
    assert "TELEGRAM_CHAT_ID=42" in content


def test_write_env_updates_existing_key_without_duplicating(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=old\nOTHER=keep\n", encoding="utf-8")
    write_env(path, {"TELEGRAM_BOT_TOKEN": "new"})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert "TELEGRAM_BOT_TOKEN=new" in lines
    assert "OTHER=keep" in lines
    assert len([l for l in lines if l.startswith("TELEGRAM_BOT_TOKEN")]) == 1


# --- enable_poster ---


def test_enable_poster_creates_the_missing_sections(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("chain:\n  name: robinhood\n", encoding="utf-8")

    enable_poster(path, "RobinHoodKnower")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["poster"]["enabled"] is True
    assert data["twitter"]["post_as"] == "RobinHoodKnower"
    assert data["chain"]["name"] == "robinhood"


def test_enable_poster_keeps_the_owners_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "mint:\n  min_unique_minters: 40\n"
        "poster:\n  max_posts_per_day: 2\n"
        "twitter:\n  enabled: true\n  watchlist: [alice]\n",
        encoding="utf-8",
    )

    enable_poster(path, "acc")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["mint"]["min_unique_minters"] == 40
    assert data["poster"]["max_posts_per_day"] == 2
    assert data["poster"]["enabled"] is True
    assert data["twitter"]["enabled"] is True
    assert data["twitter"]["watchlist"] == ["alice"]


def test_enable_poster_works_without_an_existing_file(tmp_path):
    path = tmp_path / "sub" / "config.yaml"
    enable_poster(path, "acc")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["poster"]["enabled"] is True


def test_enabled_config_loads_into_settings(tmp_path):
    from wlparser.config import load_settings

    path = tmp_path / "config.yaml"
    enable_poster(path, "acc")
    settings = load_settings(path, tmp_path / "missing.env")
    assert settings.poster.enabled is True
    assert settings.twitter.post_as == "acc"


# --- set_llm_model ---


def test_set_llm_model_creates_the_missing_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("chain:\n  name: robinhood\n", encoding="utf-8")

    set_llm_model(path, "qwen2.5:3b")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["llm"]["enabled"] is True
    assert data["llm"]["model"] == "qwen2.5:3b"
    assert data["chain"]["name"] == "robinhood"


def test_set_llm_model_keeps_the_owners_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "poster:\n  enabled: true\n"
        "llm:\n  base_url: http://host.docker.internal:11434/v1\n"
        "  filter_alerts: false\n",
        encoding="utf-8",
    )

    set_llm_model(path, "qwen2.5:3b")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["poster"]["enabled"] is True
    assert data["llm"]["base_url"] == "http://host.docker.internal:11434/v1"
    assert data["llm"]["filter_alerts"] is False
    assert data["llm"]["model"] == "qwen2.5:3b"
    assert data["llm"]["enabled"] is True


def test_set_llm_model_works_without_an_existing_file(tmp_path):
    path = tmp_path / "sub" / "config.yaml"
    set_llm_model(path, "qwen2.5:3b")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["llm"]["model"] == "qwen2.5:3b"


def test_set_llm_model_loads_into_settings(tmp_path):
    from wlparser.config import load_settings

    path = tmp_path / "config.yaml"
    set_llm_model(path, "qwen2.5:3b")
    settings = load_settings(path, tmp_path / "missing.env")
    assert settings.llm.enabled is True
    assert settings.llm.model == "qwen2.5:3b"


# --- use_local_db ---


def test_use_local_db_clears_the_url_and_keeps_other_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "db_url: libsql://example.turso.io\n"
        "poster:\n  enabled: true\n",
        encoding="utf-8",
    )

    use_local_db(path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["db_url"] == ""
    assert data["poster"]["enabled"] is True


def test_use_local_db_works_without_an_existing_file(tmp_path):
    path = tmp_path / "sub" / "config.yaml"
    use_local_db(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["db_url"] == ""


def test_use_local_db_loads_into_settings_as_local(tmp_path):
    from wlparser.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text("db_url: libsql://example.turso.io\n", encoding="utf-8")

    use_local_db(path)

    settings = load_settings(path, tmp_path / "missing.env")
    assert settings.db_url == ""
