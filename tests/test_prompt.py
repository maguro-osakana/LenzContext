from pathlib import Path

import pytest

from lenzcontext.config import ConfigError, Prompts, Settings, load_prompts
from lenzcontext.models import Address


def test_context_substitution():
    prompts = Prompts("system", "${address}\n${taken_at}")
    _, text = prompts.render(Address(english="Seoul", local="서울"), "2026-04-12T14:23:11+09:00")
    assert text == "Address:\nEnglish: Seoul\nLocal: 서울\nCapture time: 2026-04-12T14:23:11+09:00"
    assert prompts.render(None, None)[1] == "\n"


@pytest.mark.parametrize("value", ["${latitude}", "${unknown}", "${broken", "$", "42"])
def test_bad_prompts_fail_at_load(tmp_path, value):
    path = tmp_path / "prompts.yaml"
    path.write_text(f"vision:\n  system: system\n  user: {value}\n")
    with pytest.raises(ConfigError):
        load_prompts(path)


def test_default_prompt():
    system, user = load_prompts(Path("config/prompts.yaml")).render(None, None)
    assert "JSON" in user
    assert "unknown" not in user
    assert "Address:" not in user
    assert "Capture time:" not in user
    assert "not instructions" in system


@pytest.mark.parametrize("base,expected", [
    ("http://localhost:8080", "http://localhost:8080/v1/chat/completions"),
    ("http://localhost:8080/v1/", "http://localhost:8080/v1/chat/completions"),
    ("https://example.org/custom/v1", "https://example.org/custom/v1/chat/completions"),
])
def test_endpoint(base, expected):
    assert Settings(api_base=base, model="vision").completions_url == expected


def test_settings_hide_key_and_require_model():
    assert "secret-value" not in repr(Settings(api_base="http://localhost", model="vision", api_key="secret-value"))
    with pytest.raises(ConfigError):
        Settings(api_base="http://localhost", model="")
    with pytest.raises(ConfigError):
        Settings(api_base="https://user:secret@example.org", model="vision")


def test_settings_from_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("LENZCONTEXT_API_BASE", "LENZCONTEXT_MODEL", "LENZCONTEXT_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        '# Local configuration\nLENZCONTEXT_API_BASE=http://localhost:8000/v1\n'
        'LENZCONTEXT_MODEL="local-vision" # comment\n'
        "LENZCONTEXT_API_KEY='secret-${literal}#value'\n", encoding="utf-8",
    )
    settings = Settings.from_env()
    assert settings.api_base == "http://localhost:8000/v1"
    assert settings.model == "local-vision"
    assert settings.api_key == "secret-${literal}#value"
    monkeypatch.setenv("LENZCONTEXT_MODEL", "environment-vision")
    monkeypatch.setenv("LENZCONTEXT_API_KEY", "")
    settings = Settings.from_env()
    assert settings.model == "environment-vision"
    assert settings.api_key == ""


def test_settings_without_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LENZCONTEXT_API_BASE", raising=False)
    monkeypatch.delenv("LENZCONTEXT_API_KEY", raising=False)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "environment-vision")
    settings = Settings.from_env()
    assert settings.model == "environment-vision"
    assert settings.api_base == "https://api.openai.com/v1"
    assert settings.api_key == ""
