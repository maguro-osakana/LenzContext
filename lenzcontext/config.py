"""Environment settings and strict string.Template prompt loading."""

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

from .models import Address


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Reasoning:
    enabled: bool
    token_budget: int = 512

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("vision.reasoning.enabled must be true or false")
        if type(self.token_budget) is not int or self.token_budget <= 0:
            raise ConfigError("vision.reasoning.token_budget must be a positive integer")

    def request_parameters(self) -> dict:
        if not self.enabled:
            return {"reasoning_effort": "none"}
        return {
            # Enable the verified thinking mode; control its limit separately.
            "reasoning_effort": "low",
            "thinking_token_budget": self.token_budget,
        }


@dataclass(frozen=True)
class Prompts:
    system: str
    user: str
    reasoning: Reasoning | None = None

    def render(self, address: Address | None, taken_at: str | None,
               description_language: str = "English") -> tuple[str, str]:
        context = {
            "address": (
                f"Address:\nEnglish: {address.english}\nLocal: {address.local}"
                if address else ""
            ),
            "taken_at": f"Capture time: {taken_at}" if taken_at else "",
            "description_language": description_language,
        }
        return tuple(Template(value).substitute(context) for value in (self.system, self.user))


def load_prompts(path: Path | None = None) -> Prompts:
    if path is None:
        candidates = (
            Path("config/prompts.yaml"),
            Path(__file__).resolve().parent.parent / "config/prompts.yaml",
            Path(sys.prefix) / "share/lenzcontext/config/prompts.yaml",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        vision = data["vision"]
        reasoning = None
        if "reasoning" in vision:
            raw = vision["reasoning"]
            if (not isinstance(raw, dict) or "enabled" not in raw
                    or set(raw) - {"enabled", "token_budget"}):
                raise ConfigError("vision.reasoning requires enabled and accepts only enabled, token_budget")
            reasoning = Reasoning(**raw)
        prompts = Prompts(system=vision["system"], user=vision["user"], reasoning=reasoning)
        if not all(isinstance(v, str) and v.strip() for v in (prompts.system, prompts.user)):
            raise ValueError("vision.system and vision.user must be nonempty strings")
        prompts.render(None, None)
        return prompts
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError("invalid prompt configuration: check YAML and template placeholders") from exc


@dataclass(frozen=True)
class Settings:
    api_base: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout: float = 120.0
    structured_output: bool = False
    retries: int = 5
    geonames_db: Path = Path("data/geonames.db")
    prompt_config: Path | None = None
    description_language: str = "English"

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.api_base)
            valid_url = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("API base must be an HTTP(S) URL without credentials, query, or fragment")
        if not self.model.strip():
            raise ConfigError("set LENZCONTEXT_MODEL to a vision-capable model")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigError("API timeout must be positive and finite")
        if type(self.retries) is not int or self.retries < 0:
            raise ConfigError("retries must be a non-negative integer")
        if not self.description_language.strip() or any(char in self.description_language for char in "\r\n"):
            raise ConfigError("description language must be a nonempty single-line language name")
        if "\r" in self.api_key or "\n" in self.api_key:
            raise ConfigError("API key contains an invalid newline")

    @property
    def completions_url(self) -> str:
        base = self.api_base.rstrip("/")
        if not urlsplit(base).path:
            base += "/v1"
        return base + "/chat/completions"

    @classmethod
    def from_env(cls, *, structured_output: bool = False, timeout: float | None = None,
                 retries: int | None = None, geonames_db: Path | None = None,
                 prompt_config: Path | None = None,
                 description_language: str | None = None) -> "Settings":
        try:
            values = {
                key: value for key, value in dotenv_values(
                    Path.cwd() / ".env", interpolate=False, encoding="utf-8-sig",
                ).items() if value is not None
            }
        except (OSError, UnicodeError) as exc:
            raise ConfigError("could not read .env in the working directory") from exc
        values.update(os.environ)

        # Select overrides before conversion so unused environment values cannot
        # invalidate an explicitly configured CLI option (including retries=0).
        if timeout is None:
            try:
                timeout = float(values.get("LENZCONTEXT_TIMEOUT", "120"))
            except ValueError:
                raise ConfigError("LENZCONTEXT_TIMEOUT must be a positive finite number") from None
        if retries is None:
            try:
                retries = int(values.get("LENZCONTEXT_RETRIES", "5"))
            except ValueError:
                raise ConfigError("LENZCONTEXT_RETRIES must be a non-negative integer") from None

        def configured_path(override: Path | None, key: str, default: str | None) -> Path | None:
            if override is not None:
                return override
            value = values.get(key, default)
            if value is None:
                return None
            if not value.strip() or "\x00" in value:
                raise ConfigError(f"{key} must be a nonempty valid path")
            return Path(value)

        return cls(
            api_base=values.get("LENZCONTEXT_API_BASE", "https://api.openai.com/v1"),
            api_key=values.get("LENZCONTEXT_API_KEY", ""),
            model=values.get("LENZCONTEXT_MODEL", ""),
            timeout=timeout,
            retries=retries,
            structured_output=structured_output,
            geonames_db=configured_path(geonames_db, "LENZCONTEXT_GEONAMES_DB", "data/geonames.db"),
            prompt_config=configured_path(prompt_config, "LENZCONTEXT_PROMPT_CONFIG", None),
            description_language=(description_language if description_language is not None else
                                  values.get("LENZCONTEXT_DESCRIPTION_LANGUAGE", "English")).strip(),
        )
