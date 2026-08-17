import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    def __init__(self, *, title, username, broadcast=True, megagroup=False):
        self.title = title
        self.username = username
        self.broadcast = broadcast
        self.megagroup = megagroup


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
            for message in self.histories[entity.username]:
                self.seen_messages.append((entity.username, message.id))
                yield message

        return messages()


class TelegramCollectionTests(unittest.IsolatedAsyncioTestCase):
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

    def test_rejects_missing_required_value(self):
        (self.root / ".env").write_text(VALID_ENV.replace("OPENAI_MODEL=gpt-test\n", ""), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "OPENAI_MODEL"):
            load_settings(self.root, {})


class ChannelParsingTests(unittest.TestCase):
    def test_ignores_comments_and_deduplicates_case_insensitively(self):
        usernames = parse_channel_urls("# sources\n\nhttps://t.me/News\nhttps://t.me/news\nhttps://t.me/Second\n")

        self.assertEqual(usernames, ["News", "Second"])

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


class LocalInputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_loads_stripped_prompt_and_channels_from_given_root(self):
        (self.root / "PROMPT.md").write_text("\n Сделай краткую сводку. \n", encoding="utf-8")
        (self.root / "DIGEST.md").write_text("# channels\nhttps://t.me/News\n", encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
