"""Local configuration and source parsing for Telegram Digest."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse
import argparse
import asyncio
import os
import re
import sys

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None


class ConfigError(ValueError):
    """Raised when local Digest configuration is invalid."""


class CollectionError(ValueError):
    """Raised when a requested Telegram source is not a public broadcast channel."""


class GenerationError(ValueError):
    """Raised when the model returns no usable digest text."""


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str


@dataclass(frozen=True)
class Post:
    channel_title: str
    channel_username: str
    published_at: datetime
    url: str
    text: str


REQUIRED_SETTINGS = (
    "TG_API_ID",
    "TG_API_HASH",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
)
USERNAME_RE = re.compile(r"[A-Za-z0-9_]+$")
PROJECT_ROOT = Path(__file__).resolve().parent


def _openai_client_factory() -> Callable[..., object]:
    if OpenAI is None:
        from openai import OpenAI as imported_openai

        return imported_openai

    return OpenAI


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
    try:
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
    except ValueError as error:
        raise ConfigError("OPENAI_BASE_URL must be an HTTP(S) URL ending in /v1") from error
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
        source = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        if source.startswith("@"):
            username = source[1:]
            valid = bool(USERNAME_RE.fullmatch(username))
        else:
            try:
                parsed = urlparse(line)
                hostname = parsed.hostname
            except ValueError as error:
                raise ConfigError(f"Invalid channel source on line {number}") from error
            path_match = re.fullmatch(r"/([A-Za-z0-9_]+)/?", parsed.path)
            username = path_match.group(1) if path_match else ""
            valid = (
                parsed.scheme == "https"
                and hostname == "t.me"
                and parsed.netloc.lower() == "t.me"
                and "?" not in line
                and "#" not in line
                and not parsed.query
                and not parsed.fragment
                and not parsed.params
                and bool(USERNAME_RE.fullmatch(username))
            )
        if not valid:
            raise ConfigError(f"Invalid channel source on line {number}")
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
        raise ConfigError("DIGEST.md has no channel sources")
    return usernames


def _telegram_client_factory() -> Callable[..., object]:
    from telethon import TelegramClient

    return TelegramClient


def _public_broadcast_details(
    entity: object,
    requested_username: str,
) -> tuple[str, str]:
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    if (
        not getattr(entity, "broadcast", False)
        or getattr(entity, "megagroup", False)
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise CollectionError("Source is not a public broadcast channel")

    if not isinstance(username, str) or not username.strip():
        for alias in getattr(entity, "usernames", None) or ():
            alias_username = getattr(alias, "username", None)
            if (
                getattr(alias, "active", False)
                and isinstance(alias_username, str)
                and alias_username.strip()
                and alias_username.casefold() == requested_username.casefold()
            ):
                username = alias_username
                break
    if not isinstance(username, str) or not username.strip():
        raise CollectionError("Source is not a public broadcast channel")
    return title, username


async def collect_posts(
    root: Path,
    settings: Settings,
    usernames: list[str],
    cutoff: datetime,
    as_of: datetime,
    *,
    client_factory: Callable[..., object] | None = None,
) -> list[Post]:
    """Collect textual public-channel messages in the inclusive UTC time window."""
    factory = client_factory or _telegram_client_factory()
    client = factory(str(Path(root) / "telegram"), settings.tg_api_id, settings.tg_api_hash)
    posts: list[Post] = []
    try:
        await client.start()
        for requested_username in usernames:
            entity = await client.get_entity(requested_username)
            title, resolved_username = _public_broadcast_details(
                entity,
                requested_username,
            )
            async for message in client.iter_messages(entity):
                published_at = message.date.astimezone(timezone.utc)
                if published_at > as_of:
                    continue
                if published_at < cutoff:
                    break
                raw_text = message.raw_text
                if not isinstance(raw_text, str):
                    continue
                text = raw_text.strip()
                if not text:
                    continue
                posts.append(
                    Post(
                        title,
                        resolved_username,
                        published_at,
                        f"https://t.me/{resolved_username}/{message.id}",
                        text,
                    )
                )
    finally:
        await client.disconnect()
    return sorted(
        posts,
        key=lambda post: (
            post.published_at,
            post.channel_username.casefold(),
            post.url,
            post.text,
            post.channel_title,
        ),
    )


def format_posts(posts: list[Post]) -> str:
    """Render collected source posts as deterministic, user-facing plain text."""
    if not posts:
        raise ValueError("Cannot format an empty post list")
    blocks = []
    for number, post in enumerate(posts, start=1):
        timestamp = post.published_at.astimezone(timezone.utc).isoformat()
        blocks.append(
            f"{number}. Channel: {post.channel_title} (@{post.channel_username})\n"
            f"Published: {timestamp}\n"
            f"URL: {post.url}\n"
            f"Text:\n{post.text}"
        )
    return "Telegram posts collected:\n\n" + "\n\n".join(blocks)


def generate_digest(
    settings: Settings,
    prompt: str,
    formatted_posts: str,
    *,
    client_factory: Callable[..., object] | None = OpenAI,
) -> str:
    """Request one Markdown digest from the configured OpenAI-compatible API."""
    factory = client_factory or _openai_client_factory()
    client = factory(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": formatted_posts},
        ],
    )
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        raise GenerationError("OpenAI returned an invalid response") from error
    if not isinstance(content, str) or not content.strip():
        raise GenerationError("OpenAI returned an empty response")
    return content


def save_digest(root: Path, content: str, generated_at: datetime) -> Path:
    """Persist a successful model response beneath the requested project root."""
    if not content:
        raise ValueError("Digest content is empty")
    timestamp = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%SZ")
    output_path = Path(root) / "output" / f"{timestamp}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def run(
    days: int,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] = os.environ,
    *,
    telegram_client_factory: Callable[..., object] | None = None,
    openai_client_factory: Callable[..., object] | None = None,
    now_factory: Callable[[], datetime] | None = None,
    printer: Callable[[str], None] = print,
) -> Path:
    """Collect one time window, generate a digest, and persist it on success."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ConfigError("--days must be a positive integer")
    root = Path(root)
    settings = load_settings(root, environ)
    prompt = load_prompt(root)
    usernames = load_channel_usernames(root)
    as_of = (now_factory or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    try:
        posts = asyncio.run(
            collect_posts(
                root,
                settings,
                usernames,
                as_of - timedelta(days=days),
                as_of,
                client_factory=telegram_client_factory,
            )
        )
    except CollectionError:
        raise
    except Exception as error:
        raise CollectionError("Unable to collect posts") from error
    if not posts:
        raise CollectionError("No posts found in the requested time window")
    formatted_posts = format_posts(posts)
    try:
        content = generate_digest(
            settings,
            prompt,
            formatted_posts,
            client_factory=openai_client_factory,
        )
    except GenerationError:
        raise
    except Exception as error:
        if error.__class__.__name__ == "BadRequestError":
            raise
        raise GenerationError("OpenAI request failed") from error
    output_path = save_digest(root, content, as_of)
    printer(f"Channels: {len(usernames)}")
    printer(f"Posts: {len(posts)}")
    printer(f"Output: {output_path.resolve()}")
    return output_path


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--days must be a positive integer") from error
    if days <= 0:
        raise argparse.ArgumentTypeError("--days must be a positive integer")
    return days


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and report safe, concise operational errors."""
    parser = argparse.ArgumentParser(description="Generate a local Telegram digest.")
    parser.add_argument("--days", required=True, type=_positive_days, help="positive number of UTC days to collect")
    args = parser.parse_args(argv)
    try:
        run(args.days)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1
    except CollectionError:
        print("Telegram error: unable to collect posts", file=sys.stderr)
        return 1
    except GenerationError:
        print("OpenAI error: unable to generate digest", file=sys.stderr)
        return 1
    except Exception as error:
        module = error.__class__.__module__
        name = error.__class__.__name__
        if name == "BadRequestError":
            print("OpenAI request may exceed the context limit; reduce --days or sources.", file=sys.stderr)
            return 1
        if module.startswith("openai"):
            print("OpenAI error: unable to generate digest", file=sys.stderr)
            return 1
        if module.startswith("telethon"):
            print("Telegram error: unable to collect posts", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
