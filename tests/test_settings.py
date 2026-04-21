from __future__ import annotations

from blackbox_service.settings import load_settings


def test_settings_use_env_file_key_not_terminal_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=file-key",
                "BLACKBOX_AGENT_MODEL=claude-opus-4-7",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "terminal-key")
    monkeypatch.setenv("GEMINI_API_KEY", "terminal-gemini-key")

    settings = load_settings(env_file=env_file)

    assert settings.anthropic_api_key == "file-key"
    assert settings.agent_model == "claude-opus-4-7"


def test_settings_loads_gemini_key_from_env_file_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GEMINI_API_KEY=file-gemini-key",
                "GOOGLE_API_KEY=legacy-google-key",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "terminal-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "terminal-google-key")

    settings = load_settings(env_file=env_file)

    assert settings.gemini_api_key == "file-gemini-key"


def test_settings_default_model_is_opus_when_not_set(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = load_settings(env_file=env_file)

    assert settings.agent_model == "claude-opus-4-7"
    assert settings.default_target_url == "http://127.0.0.1:3000/#/"
