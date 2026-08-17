"""Local configuration and source parsing for Telegram Digest."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
import re


class ConfigError(ValueError):
    """Raised when local Digest configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str


REQUIRED_SETTINGS = (
    "TG_API_ID",
    "TG_API_HASH",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
)
USERNAME_RE = re.compile(r"[A-Za-z0-9_]+$")


def _read_text(root: Path, filename: str) -> str:
    path = Path(root) / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(f"Missing {filename}") from error


def parse_env(text: str) -> dict[str, str]:
    """Parse a small UTF-8 .env file without changing process environment."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {number}")
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise ConfigError(f"Invalid .env line {number}")
            value = value[1:-1]
        elif value.endswith(("'", '"')):
            raise ConfigError(f"Invalid .env line {number}")
        values[key] = value
    return values


def load_settings(root: Path, environ: Mapping[str, str]) -> Settings:
    values = parse_env(_read_text(root, ".env"))
    values.update({key: value for key, value in environ.items() if key in REQUIRED_SETTINGS})
    missing = [key for key in REQUIRED_SETTINGS if not values.get(key)]
    if missing:
        raise ConfigError(f"Missing required setting: {missing[0]}")
    try:
        tg_api_id = int(values["TG_API_ID"])
    except ValueError as error:
        raise ConfigError("TG_API_ID must be an integer") from error

    base_url = values["OPENAI_BASE_URL"]
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not path.endswith("/v1"):
        raise ConfigError("OPENAI_BASE_URL must be an HTTP(S) URL ending in /v1")

    return Settings(
        tg_api_id=tg_api_id,
        tg_api_hash=values["TG_API_HASH"],
        openai_base_url=base_url,
        openai_api_key=values["OPENAI_API_KEY"],
        openai_model=values["OPENAI_MODEL"],
    )


def parse_channel_urls(text: str) -> list[str]:
    usernames: list[str] = []
    seen: set[str] = set()
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        path_match = re.fullmatch(r"/([A-Za-z0-9_]+)/?", parsed.path)
        username = path_match.group(1) if path_match else ""
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "t.me"
            and parsed.netloc.lower() == "t.me"
            and not parsed.query
            and not parsed.fragment
            and bool(USERNAME_RE.fullmatch(username))
        )
        if not valid:
            raise ConfigError(f"Invalid channel URL on line {number}")
        normalized = username.lower()
        if normalized not in seen:
            seen.add(normalized)
            usernames.append(username)
    return usernames


def load_prompt(root: Path) -> str:
    prompt = _read_text(root, "PROMPT.md").strip()
    if not prompt:
        raise ConfigError("PROMPT.md is empty")
    return prompt


def load_channel_usernames(root: Path) -> list[str]:
    usernames = parse_channel_urls(_read_text(root, "DIGEST.md"))
    if not usernames:
        raise ConfigError("DIGEST.md has no channel URLs")
    return usernames
