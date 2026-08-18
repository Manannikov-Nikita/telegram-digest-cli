import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import subprocess
import sys

import main as digest_main

from main import (
    CollectionError,
    ConfigError,
    Post,
    Settings,
    collect_posts,
    format_posts,
    load_channel_usernames,
    load_prompt,
    load_settings,
    parse_channel_urls,
)


VALID_ENV = """\
TG_API_ID=12345
TG_API_HASH=file-hash
OPENAI_BASE_URL='https://api.example.test/v1/'
OPENAI_API_KEY="file-key"
OPENAI_MODEL=gpt-test
"""


UTC = timezone.utc


class FakeEntity:
    def __init__(
        self,
        *,
        title,
        username,
        broadcast=True,
        megagroup=False,
        usernames=None,
    ):
        self.title = title
        self.username = username
        self.broadcast = broadcast
        self.megagroup = megagroup
        self.usernames = usernames


class FakePublicUsername:
    def __init__(self, username, *, active=True):
        self.username = username
        self.active = active


class FakeMessage:
    def __init__(self, message_id, date, raw_text):
        self.id = message_id
        self.date = date
        self.raw_text = raw_text


class FakeTelegramClient:
    def __init__(self, entities, histories, error_by_username=None, history_error_by_username=None):
        self.entities = entities
        self.histories = histories
        self.error_by_username = error_by_username or {}
        self.history_error_by_username = history_error_by_username or {}
        self.events = []
        self.seen_messages = []

    async def start(self):
        self.events.append("start")

    async def disconnect(self):
        self.events.append("disconnect")

    async def get_entity(self, username):
        self.events.append(f"resolve:{username}")
        if username in self.error_by_username:
            raise self.error_by_username[username]
        return self.entities[username]

    def iter_messages(self, entity):
        async def messages():
            if entity.username in self.history_error_by_username:
                raise self.history_error_by_username[entity.username]
            history = self.histories.get(entity)
            if history is None:
                history = self.histories[entity.username]
            for message in history:
                self.seen_messages.append((entity.username, message.id))
                yield message

        return messages()


class TelegramCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_matching_active_alternate_username_for_public_channel(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        entity = FakeEntity(
            title="News",
            username=None,
            usernames=[
                FakePublicUsername("OtherAlias"),
                FakePublicUsername("RequestedAlias"),
            ],
        )
        client = FakeTelegramClient(
            {"requestedalias": entity},
            {entity: [FakeMessage(7, point, "alternate username post")]},
        )

        try:
            posts = await collect_posts(
                Path("/tmp/digest-root"),
                settings,
                ["requestedalias"],
                point,
                point,
                client_factory=lambda *args: client,
            )
        except CollectionError as error:
            self.fail(f"matching active alternate username must be accepted: {error}")

        self.assertEqual(
            posts,
            [
                Post(
                    "News",
                    "RequestedAlias",
                    point,
                    "https://t.me/RequestedAlias/7",
                    "alternate username post",
                )
            ],
        )
        self.assertEqual(client.events, ["start", "resolve:requestedalias", "disconnect"])

    async def test_rejects_inactive_or_unrelated_alternate_usernames(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        invalid_aliases = (
            [FakePublicUsername("requestedalias", active=False)],
            [FakePublicUsername("differentalias")],
        )

        for aliases in invalid_aliases:
            with self.subTest(aliases=[alias.username for alias in aliases]):
                entity = FakeEntity(title="News", username=None, usernames=aliases)
                client = FakeTelegramClient({"requestedalias": entity}, {entity: []})

                with self.assertRaises(CollectionError):
                    await collect_posts(
                        Path("/tmp/digest-root"),
                        settings,
                        ["requestedalias"],
                        point,
                        point,
                        client_factory=lambda *args: client,
                    )

                self.assertEqual(
                    client.events,
                    ["start", "resolve:requestedalias", "disconnect"],
                )

    async def test_collects_utc_window_with_lifecycle_urls_and_deterministic_order(self):
        root = Path("/tmp/digest-root")
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        cutoff = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        as_of = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
        first = FakeEntity(title="First title", username="ResolvedFirst")
        second = FakeEntity(title="Second title", username="resolved_second")
        client = FakeTelegramClient(
            {"first": first, "second": second},
            {
                "ResolvedFirst": [
                    FakeMessage(99, as_of + timedelta(seconds=1), "future"),
                    FakeMessage(20, as_of, "  at as-of  "),
                    FakeMessage(19, datetime(2026, 8, 10, 13, 0, tzinfo=UTC), ""),
                    FakeMessage(18, cutoff, "caption text"),
                    FakeMessage(17, cutoff - timedelta(seconds=1), "too old"),
                    FakeMessage(16, cutoff - timedelta(minutes=1), "not reached"),
                ],
                "resolved_second": [
                    FakeMessage(11, datetime(2026, 8, 10, 13, 0, tzinfo=UTC), "second channel"),
                    FakeMessage(10, cutoff - timedelta(seconds=1), "too old"),
                ],
            },
        )

        def client_factory(*args):
            self.assertEqual(args, (str(root / "telegram"), 123, "hash"))
            return client

        posts = await collect_posts(
            root,
            settings,
            ["first", "second"],
            cutoff,
            as_of,
            client_factory=client_factory,
        )

        self.assertEqual(client.events, ["start", "resolve:first", "resolve:second", "disconnect"])
        self.assertEqual(
            client.seen_messages,
            [
                ("ResolvedFirst", 99),
                ("ResolvedFirst", 20),
                ("ResolvedFirst", 19),
                ("ResolvedFirst", 18),
                ("ResolvedFirst", 17),
                ("resolved_second", 11),
                ("resolved_second", 10),
            ],
        )
        self.assertEqual(
            posts,
            [
                Post("First title", "ResolvedFirst", cutoff, "https://t.me/ResolvedFirst/18", "caption text"),
                Post("Second title", "resolved_second", datetime(2026, 8, 10, 13, 0, tzinfo=UTC), "https://t.me/resolved_second/11", "second channel"),
                Post("First title", "ResolvedFirst", as_of, "https://t.me/ResolvedFirst/20", "at as-of"),
            ],
        )

    async def test_skips_service_messages_without_raw_text_and_keeps_collecting(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        entity = FakeEntity(title="News", username="news")
        client = FakeTelegramClient(
            {"news": entity},
            {
                "news": [
                    FakeMessage(2, point, None),
                    FakeMessage(1, point, "  media caption  "),
                ]
            },
        )

        try:
            posts = await collect_posts(
                Path("/tmp/digest-root"),
                settings,
                ["news"],
                point,
                point,
                client_factory=lambda *args: client,
            )
        except AttributeError as error:
            self.fail(f"service messages must be skipped: {error}")

        self.assertEqual(
            posts,
            [Post("News", "news", point, "https://t.me/news/1", "media caption")],
        )
        self.assertEqual(client.events, ["start", "resolve:news", "disconnect"])

    async def test_rejects_non_public_broadcast_entities_and_disconnects(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        invalid_entities = (
            FakeEntity(title="User", username="user", broadcast=False),
            FakeEntity(title="Group", username="group", broadcast=False),
            FakeEntity(title="Mega", username="mega", broadcast=True, megagroup=True),
            FakeEntity(title="Private", username=None),
            FakeEntity(title="", username="no_title"),
        )

        for entity in invalid_entities:
            with self.subTest(entity=entity.title):
                client = FakeTelegramClient({"source": entity}, {entity.username: []})
                with self.assertRaises(CollectionError):
                    await collect_posts(
                        Path("/tmp/digest-root"),
                        settings,
                        ["source"],
                        point,
                        point,
                        client_factory=lambda *args: client,
                    )
                self.assertEqual(client.events, ["start", "resolve:source", "disconnect"])

    async def test_fails_fast_and_disconnects_on_resolution_and_history_errors(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        resolution_client = FakeTelegramClient(
            {},
            {},
            error_by_username={"broken": RuntimeError("cannot resolve")},
        )
        with self.assertRaisesRegex(RuntimeError, "cannot resolve"):
            await collect_posts(
                Path("/tmp/digest-root"),
                settings,
                ["broken", "never-resolved"],
                point,
                point,
                client_factory=lambda *args: resolution_client,
            )
        self.assertEqual(resolution_client.events, ["start", "resolve:broken", "disconnect"])

        entity = FakeEntity(title="News", username="news")
        history_client = FakeTelegramClient(
            {"news": entity},
            {"news": []},
            history_error_by_username={"news": RuntimeError("history failed")},
        )
        with self.assertRaisesRegex(RuntimeError, "history failed"):
            await collect_posts(
                Path("/tmp/digest-root"),
                settings,
                ["news", "never-resolved"],
                point,
                point,
                client_factory=lambda *args: history_client,
            )
        self.assertEqual(history_client.events, ["start", "resolve:news", "disconnect"])

    async def test_breaks_same_timestamp_ties_by_resolved_channel_and_url(self):
        settings = Settings(123, "hash", "https://api.example.test/v1", "key", "model")
        point = datetime(2026, 8, 10, 12, tzinfo=UTC)
        zulu = FakeEntity(title="Zulu", username="zulu")
        alpha = FakeEntity(title="Alpha", username="Alpha")
        client = FakeTelegramClient(
            {"zulu": zulu, "alpha": alpha},
            {
                "zulu": [FakeMessage(2, point, "z")],
                "Alpha": [FakeMessage(1, point, "a")],
            },
        )

        posts = await collect_posts(
            Path("/tmp/digest-root"),
            settings,
            ["zulu", "alpha"],
            point,
            point,
            client_factory=lambda *args: client,
        )

        self.assertEqual([post.url for post in posts], ["https://t.me/Alpha/1", "https://t.me/zulu/2"])

    def test_formats_posts_as_stable_data_only_numbered_blocks(self):
        post = Post(
            "Channel title",
            "channel_name",
            datetime(2026, 8, 10, 12, 30, tzinfo=UTC),
            "https://t.me/channel_name/42",
            "Verbatim\ntext",
        )

        self.assertEqual(
            format_posts([post]),
            "Telegram posts collected:\n\n"
            "1. Channel: Channel title (@channel_name)\n"
            "Published: 2026-08-10T12:30:00+00:00\n"
            "URL: https://t.me/channel_name/42\n"
            "Text:\n"
            "Verbatim\ntext",
        )

    def test_rejects_an_empty_post_list_for_formatting(self):
        with self.assertRaises(ValueError):
            format_posts([])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".env").write_text(VALID_ENV, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_loads_file_values_and_matching_quotes(self):
        settings = load_settings(self.root, {})

        self.assertEqual(settings.tg_api_id, 12345)
        self.assertEqual(settings.tg_api_hash, "file-hash")
        self.assertEqual(settings.openai_base_url, "https://api.example.test/v1/")
        self.assertEqual(settings.openai_api_key, "file-key")
        self.assertEqual(settings.openai_model, "gpt-test")

    def test_environment_values_override_file_values(self):
        settings = load_settings(
            self.root,
            {"TG_API_ID": "9", "OPENAI_MODEL": "environment-model"},
        )

        self.assertEqual(settings.tg_api_id, 9)
        self.assertEqual(settings.openai_model, "environment-model")

    def test_rejects_malformed_env_line_with_line_number(self):
        (self.root / ".env").write_text("TG_API_ID=1\nbroken\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, r"line 2"):
            load_settings(self.root, {})

    def test_rejects_missing_integer_id_and_base_url_without_v1(self):
        (self.root / ".env").write_text(VALID_ENV.replace("12345", "nope"), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "TG_API_ID"):
            load_settings(self.root, {})

        (self.root / ".env").write_text(
            VALID_ENV.replace("https://api.example.test/v1/", "https://api.example.test/api"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "OPENAI_BASE_URL"):
            load_settings(self.root, {})

    def test_rejects_a_malformed_base_url_as_a_config_error(self):
        (self.root / ".env").write_text(
            VALID_ENV.replace("https://api.example.test/v1/", "https://[::1/v1"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "OPENAI_BASE_URL"):
            load_settings(self.root, {})

    def test_rejects_missing_required_value(self):
        (self.root / ".env").write_text(VALID_ENV.replace("OPENAI_MODEL=gpt-test\n", ""), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "OPENAI_MODEL"):
            load_settings(self.root, {})


class ChannelParsingTests(unittest.TestCase):
    def test_ignores_comments_and_deduplicates_case_insensitively(self):
        usernames = parse_channel_urls("# sources\n\nhttps://t.me/News\nhttps://t.me/news\nhttps://t.me/Second\n")

        self.assertEqual(usernames, ["News", "Second"])

    def test_accepts_at_handles_and_deduplicates_across_source_forms(self):
        usernames = parse_channel_urls(
            "# sources\n@News\nhttps://t.me/news\nhttps://t.me/Second\n@SECOND\n"
        )

        self.assertEqual(usernames, ["News", "Second"])

    def test_rejects_malformed_at_handles_with_their_line_number(self):
        for value in ("@", "@@news", "@news extra", "@news/42", "@news ", "@news\t", "news"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, r"Invalid channel source on line 2"):
                    parse_channel_urls(f"@Valid\n{value}\n")

    def test_rejects_non_root_or_non_telegram_urls_and_prose(self):
        for value in (
            "https://t.me/+invite",
            "https://t.me/news/42",
            "https://t.me//news",
            "https://t.me/news?x=1",
            "https://t.me/news?",
            "https://t.me/news#",
            "https://t.me/news;param",
            "http://t.me/news",
            "https://example.com/news",
            "a channel to read",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_channel_urls(value)

    def test_rejects_a_malformed_url_with_its_line_number(self):
        with self.assertRaisesRegex(ConfigError, r"Invalid channel source on line 2"):
            parse_channel_urls("https://t.me/News\nhttps://[::1\n")


class LocalInputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_loads_stripped_prompt_and_channels_from_given_root(self):
        (self.root / "PROMPT.md").write_text("\n Сделай краткую сводку. \n", encoding="utf-8")
        (self.root / "DIGEST.md").write_text("# channels\n@News\n", encoding="utf-8")

        self.assertEqual(load_prompt(self.root), "Сделай краткую сводку.")
        self.assertEqual(load_channel_usernames(self.root), ["News"])

    def test_rejects_missing_or_empty_prompt_and_empty_sources(self):
        with self.assertRaises(ConfigError):
            load_prompt(self.root)

        (self.root / "PROMPT.md").write_text(" \n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_prompt(self.root)

        (self.root / "DIGEST.md").write_text("# no channels\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_channel_usernames(self.root)


class FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]


class FakeOpenAIClient:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class DigestGenerationTests(unittest.TestCase):
    def test_sends_one_exact_request_and_returns_verbatim_model_text(self):
        settings = Settings(123, "tg-hash", "https://api.example.test/v1", "api-key", "digest-model")
        client = FakeOpenAIClient(FakeCompletionResponse("\n# Digest\n\nExact model text.\n"))
        constructor_calls = []

        def factory(**kwargs):
            constructor_calls.append(kwargs)
            return client

        result = digest_main.generate_digest(
            settings,
            "You write concise digests.",
            "Telegram posts collected:\n\n1. Source post",
            client_factory=factory,
        )

        self.assertEqual(result, "\n# Digest\n\nExact model text.\n")
        self.assertEqual(
            constructor_calls,
            [{"base_url": "https://api.example.test/v1", "api_key": "api-key", "max_retries": 0}],
        )
        self.assertEqual(
            client.requests,
            [
                {
                    "model": "digest-model",
                    "messages": [
                        {"role": "system", "content": "You write concise digests."},
                        {"role": "user", "content": "Telegram posts collected:\n\n1. Source post"},
                    ],
                }
            ],
        )

    def test_rejects_an_empty_model_response(self):
        settings = Settings(123, "tg-hash", "https://api.example.test/v1", "api-key", "digest-model")
        client = FakeOpenAIClient(FakeCompletionResponse(""))

        with self.assertRaises(digest_main.GenerationError):
            digest_main.generate_digest(settings, "System prompt", "One post", client_factory=lambda **_: client)

    def test_rejects_a_whitespace_only_model_response(self):
        settings = Settings(123, "tg-hash", "https://api.example.test/v1", "api-key", "digest-model")
        client = FakeOpenAIClient(FakeCompletionResponse(" \n\t"))

        with self.assertRaises(digest_main.GenerationError):
            digest_main.generate_digest(settings, "System prompt", "One post", client_factory=lambda **_: client)

    def test_rejects_a_non_string_model_response(self):
        settings = Settings(123, "tg-hash", "https://api.example.test/v1", "api-key", "digest-model")
        client = FakeOpenAIClient(FakeCompletionResponse(None))

        with self.assertRaises(digest_main.GenerationError):
            digest_main.generate_digest(settings, "System prompt", "One post", client_factory=lambda **_: client)

    def test_rejects_a_completion_without_choices(self):
        settings = Settings(123, "tg-hash", "https://api.example.test/v1", "api-key", "digest-model")
        response = type("EmptyChoicesResponse", (), {"choices": []})()
        client = FakeOpenAIClient(response)

        with self.assertRaises(digest_main.GenerationError):
            digest_main.generate_digest(settings, "System prompt", "One post", client_factory=lambda **_: client)


class DigestPersistenceTests(unittest.TestCase):
    def test_writes_verbatim_utf8_text_to_a_utc_named_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved_path = digest_main.save_digest(
                root,
                "# Сводка\n\nТочный текст модели.",
                datetime(2026, 8, 17, 7, 8, 9, tzinfo=timezone(timedelta(hours=3))),
            )

            self.assertEqual(saved_path, root / "output" / "2026-08-17_04-08-09Z.md")
            self.assertEqual(saved_path.read_text(encoding="utf-8"), "# Сводка\n\nТочный текст модели.")

    def test_refuses_empty_content_without_creating_an_output_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            with self.assertRaises(ValueError):
                digest_main.save_digest(root, "", datetime(2026, 8, 17, tzinfo=UTC))

            self.assertFalse((root / "output").exists())


class DigestRunTests(unittest.TestCase):
    def test_runs_the_real_collection_flow_and_prints_only_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            as_of = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, as_of, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse("# Saved digest"))
            lines = []

            output_path = digest_main.run(
                2,
                root=root,
                environ={},
                telegram_client_factory=lambda *args: telegram_client,
                openai_client_factory=lambda **kwargs: openai_client,
                now_factory=lambda: as_of,
                printer=lines.append,
            )

            self.assertEqual(output_path, root / "output" / "2026-08-17_12-00-00Z.md")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "# Saved digest")
            self.assertEqual(telegram_client.events, ["start", "resolve:News", "disconnect"])
            self.assertEqual(len(openai_client.requests), 1)
            self.assertEqual(lines, [f"Channels: 1", f"Posts: 1", f"Output: {output_path.resolve()}"])

    def test_rejects_nonpositive_days_before_touching_telegram_or_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            network_calls = []

            def telegram_factory(*args):
                network_calls.append(args)
                raise AssertionError("Telegram must not start")

            for days in (0, -1):
                with self.subTest(days=days):
                    with self.assertRaisesRegex(ConfigError, "positive"):
                        digest_main.run(days, root=root, environ={}, telegram_client_factory=telegram_factory)
            self.assertEqual(network_calls, [])
            self.assertFalse((root / "output").exists())

    def test_refuses_zero_collected_posts_without_calling_the_model_or_writing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": []},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse("must not be used"))

            with self.assertRaisesRegex(CollectionError, "No posts"):
                digest_main.run(
                    1,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                )

            self.assertEqual(openai_client.requests, [])
            self.assertFalse((root / "output").exists())

    def test_telegram_failure_creates_no_output_and_never_calls_the_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            telegram_client = FakeTelegramClient({}, {}, error_by_username={"News": RuntimeError("unavailable")})
            openai_client = FakeOpenAIClient(FakeCompletionResponse("must not be used"))
            lines = []

            with self.assertRaisesRegex(CollectionError, "Unable to collect posts"):
                digest_main.run(
                    1,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    printer=lines.append,
                )

            self.assertEqual(openai_client.requests, [])
            self.assertEqual(lines, [])
            self.assertFalse((root / "output").exists())

    def test_model_failure_creates_no_output_or_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, point, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse("unused"))
            openai_client.create = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable"))
            lines = []

            with self.assertRaises(digest_main.GenerationError):
                digest_main.run(
                    1,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                    printer=lines.append,
                )

            self.assertEqual(lines, [])
            self.assertFalse((root / "output").exists())

    def test_empty_model_response_creates_no_output_or_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, point, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse(""))
            lines = []

            with self.assertRaises(digest_main.GenerationError):
                digest_main.run(
                    1,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                    printer=lines.append,
                )

            self.assertEqual(lines, [])
            self.assertFalse((root / "output").exists())

    def test_whitespace_model_response_creates_no_output_or_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, point, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse(" \n\t"))
            lines = []

            with self.assertRaises(digest_main.GenerationError):
                digest_main.run(
                    1,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                    printer=lines.append,
                )

            self.assertEqual(lines, [])
            self.assertFalse((root / "output").exists())


class CommandLineTests(unittest.TestCase):
    def test_help_succeeds_and_missing_or_nonpositive_days_fail_without_stdout(self):
        script = Path(digest_main.__file__).resolve()

        help_result = subprocess.run(
            [sys.executable, str(script), "--help"], text=True, capture_output=True, check=False
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("usage:", help_result.stdout)
        self.assertEqual(help_result.stderr, "")

        for arguments in ([], ["--days", "0"], ["--days", "-1"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(script), *arguments], text=True, capture_output=True, check=False
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("--days", result.stderr)

    def test_known_operational_errors_return_safe_nonzero_stderr(self):
        cases = (
            (ConfigError("Missing PROMPT.md"), "Configuration error: Missing PROMPT.md"),
            (CollectionError("source details"), "Telegram error: unable to collect posts"),
            (digest_main.GenerationError("model details"), "OpenAI error: unable to generate digest"),
            (type("BadRequestError", (Exception,), {})("request details"), "reduce --days or sources"),
        )
        original_run = digest_main.run
        try:
            for error, expected_message in cases:
                with self.subTest(error=type(error).__name__):
                    digest_main.run = lambda days, error=error: (_ for _ in ()).throw(error)
                    stderr = StringIO()
                    with redirect_stderr(stderr):
                        self.assertEqual(digest_main.main(["--days", "1"]), 1)
                    self.assertIn(expected_message, stderr.getvalue())
                    self.assertNotIn("details", stderr.getvalue())
        finally:
            digest_main.run = original_run

    def test_connection_error_from_collection_is_safe_in_cli_and_creates_no_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            original_run = digest_main.run

            def run_with_transport_failure(days):
                return original_run(
                    days,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: (_ for _ in ()).throw(
                        ConnectionError("secret transport detail")
                    ),
                )

            digest_main.run = run_with_transport_failure
            stderr = StringIO()
            stdout = StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(digest_main.main(["--days", "1"]), 1)
            finally:
                digest_main.run = original_run

            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "Telegram error: unable to collect posts\n")
            self.assertNotIn("secret transport detail", stderr.getvalue())
            self.assertFalse((root / "output").exists())

    def test_malformed_channel_url_is_safe_at_the_real_cli_and_run_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://[::1\n", encoding="utf-8")
            original_run = digest_main.run

            def run_with_local_validation(days):
                return original_run(days, root=root, environ={})

            digest_main.run = run_with_local_validation
            stderr = StringIO()
            stdout = StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(digest_main.main(["--days", "1"]), 1)
            finally:
                digest_main.run = original_run

            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "Configuration error: Invalid channel source on line 1\n")
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse((root / "output").exists())

    def test_empty_choices_are_safe_in_cli_and_create_no_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, point, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(type("EmptyChoicesResponse", (), {"choices": []})())
            original_run = digest_main.run

            def run_with_empty_choices(days):
                return original_run(
                    days,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                )

            digest_main.run = run_with_empty_choices
            stderr = StringIO()
            stdout = StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(digest_main.main(["--days", "1"]), 1)
            finally:
                digest_main.run = original_run

            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "OpenAI error: unable to generate digest\n")
            self.assertFalse((root / "output").exists())

    def test_bad_request_from_model_keeps_the_cli_context_limit_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(VALID_ENV, encoding="utf-8")
            (root / "PROMPT.md").write_text("Make a digest.", encoding="utf-8")
            (root / "DIGEST.md").write_text("https://t.me/News\n", encoding="utf-8")
            point = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
            telegram_client = FakeTelegramClient(
                {"News": FakeEntity(title="News", username="News")},
                {"News": [FakeMessage(5, point, "Visible source post")]},
            )
            openai_client = FakeOpenAIClient(FakeCompletionResponse("unused"))
            bad_request_error = type("BadRequestError", (Exception,), {})
            openai_client.create = lambda **kwargs: (_ for _ in ()).throw(
                bad_request_error("secret request detail")
            )
            original_run = digest_main.run

            def run_with_bad_request(days):
                return original_run(
                    days,
                    root=root,
                    environ={},
                    telegram_client_factory=lambda *args: telegram_client,
                    openai_client_factory=lambda **kwargs: openai_client,
                    now_factory=lambda: point,
                )

            digest_main.run = run_with_bad_request
            stderr = StringIO()
            stdout = StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(digest_main.main(["--days", "1"]), 1)
            finally:
                digest_main.run = original_run

            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "OpenAI request may exceed the context limit; reduce --days or sources.\n",
            )
            self.assertNotIn("secret request detail", stderr.getvalue())
            self.assertFalse((root / "output").exists())

if __name__ == "__main__":
    unittest.main()
