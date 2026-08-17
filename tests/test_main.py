import tempfile
import unittest
from pathlib import Path

from main import (
    ConfigError,
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
