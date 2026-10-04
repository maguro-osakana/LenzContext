from pathlib import Path

import pytest

from lenzcontext.cli import main
from lenzcontext.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("API_BASE", "API_KEY", "MODEL", "GEONAMES_DB", "PROMPT_CONFIG",
                 "DESCRIPTION_LANGUAGE", "TIMEOUT", "RETRIES", "JOBS"):
        monkeypatch.delenv(f"LENZCONTEXT_{name}", raising=False)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "mock-vision")


def test_default_options_without_environment():
    settings = Settings.from_env()
    assert (settings.geonames_db, settings.prompt_config, settings.description_language,
            settings.timeout, settings.retries) == (Path("data/geonames.db"), None, "English", 120, 5)


def test_option_precedence(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "LENZCONTEXT_GEONAMES_DB=dotenv.db\nLENZCONTEXT_PROMPT_CONFIG=dotenv.yaml\n"
        "LENZCONTEXT_DESCRIPTION_LANGUAGE=Japanese\nLENZCONTEXT_TIMEOUT=90.5\n"
        "LENZCONTEXT_RETRIES=2\n", encoding="utf-8",
    )
    settings = Settings.from_env()
    assert (settings.geonames_db, settings.prompt_config, settings.description_language,
            settings.timeout, settings.retries) == (Path("dotenv.db"), Path("dotenv.yaml"), "Japanese", 90.5, 2)

    for name, value in {"GEONAMES_DB": "env.db", "PROMPT_CONFIG": "env.yaml",
                        "DESCRIPTION_LANGUAGE": "Korean", "TIMEOUT": "60", "RETRIES": "0"}.items():
        monkeypatch.setenv(f"LENZCONTEXT_{name}", value)
    settings = Settings.from_env()
    assert (settings.geonames_db, settings.prompt_config, settings.description_language,
            settings.timeout, settings.retries) == (Path("env.db"), Path("env.yaml"), "Korean", 60, 0)

    # Invalid lower-priority values must not prevent explicit overrides.
    for name in ("GEONAMES_DB", "PROMPT_CONFIG", "DESCRIPTION_LANGUAGE", "TIMEOUT", "RETRIES"):
        monkeypatch.setenv(f"LENZCONTEXT_{name}", "")
    settings = Settings.from_env(geonames_db=Path("cli.db"), prompt_config=Path("cli.yaml"),
                                 description_language="English", timeout=30, retries=0)
    assert (settings.geonames_db, settings.prompt_config, settings.description_language,
            settings.timeout, settings.retries) == (Path("cli.db"), Path("cli.yaml"), "English", 30, 0)


@pytest.mark.parametrize("name,value", [
    ("GEONAMES_DB", ""), ("PROMPT_CONFIG", " "), ("DESCRIPTION_LANGUAGE", ""),
    ("DESCRIPTION_LANGUAGE", "English\nJapanese"), ("TIMEOUT", "abc"),
    ("TIMEOUT", "0"), ("TIMEOUT", "-1"), ("TIMEOUT", "nan"), ("TIMEOUT", "inf"),
    ("RETRIES", ""), ("RETRIES", "1.5"), ("RETRIES", "-1"),
])
def test_invalid_selected_option(monkeypatch, name, value):
    monkeypatch.setenv(f"LENZCONTEXT_{name}", value)
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize("override", [False, True])
def test_cli_uses_resolved_defaults(monkeypatch, tmp_path, make_jpeg, analysis, override):
    for stem in ("dotenv", "cli"):
        (tmp_path / f"{stem}.yaml").write_text(
            f"vision:\n  system: {stem}\n  user: '${{description_language}}'\n", encoding="utf-8",
        )
    (tmp_path / ".env").write_text(
        "LENZCONTEXT_GEONAMES_DB=dotenv.db\nLENZCONTEXT_PROMPT_CONFIG=dotenv.yaml\n"
        "LENZCONTEXT_DESCRIPTION_LANGUAGE=Japanese\nLENZCONTEXT_TIMEOUT=90\n"
        "LENZCONTEXT_RETRIES=2\n", encoding="utf-8",
    )
    opened = []

    def unavailable_database(path):
        opened.append(path)
        raise OSError("missing database")

    def analyze(self, *args):
        assert self.prompts.system == ("cli" if override else "dotenv")
        assert self.prompts.render(None, None, self.description_language)[1] == (
            "English" if override else "Japanese")
        assert self.settings.timeout == (30 if override else 90)
        assert self.settings.retries == (0 if override else 2)
        return analysis

    monkeypatch.setattr("lenzcontext.cli.GeoNamesDatabase", unavailable_database)
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze", analyze)
    args = [str(make_jpeg()), "-o", str(tmp_path / "out.yaml")]
    if override:
        args += ["--geonames-db", "cli.db", "--prompt-config", "cli.yaml",
                 "--description-language", "English", "--timeout", "30", "--retries", "0"]
    assert main(args) == 0
    assert opened == [Path("cli.db" if override else "dotenv.db")]


def test_environment_database_cannot_be_overwritten(monkeypatch, tmp_path, make_jpeg):
    database = tmp_path / "database.db"
    database.write_bytes(b"database")
    monkeypatch.setenv("LENZCONTEXT_GEONAMES_DB", str(database))
    assert main([str(make_jpeg()), "-o", str(database)]) == 2
    assert database.read_bytes() == b"database"
