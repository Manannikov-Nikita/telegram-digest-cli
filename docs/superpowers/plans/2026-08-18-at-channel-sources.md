# `@username` Channel Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `DIGEST.md` identify public Telegram channels with either `@username` or `https://t.me/username` while preserving strict validation and deterministic deduplication.

**Architecture:** Extend the existing `parse_channel_urls(text: str) -> list[str]` function with an explicit `@username` branch and keep the existing strict URL branch. Both branches produce a plain username and feed the existing case-insensitive deduplication and unchanged Telethon collection flow.

**Tech Stack:** Python 3.11+, standard library (`re`, `urllib.parse`, `unittest`), Telethon 1.44.0.

## Global Constraints

- Accept exactly `@username` and `https://t.me/username`; retain the existing optional trailing slash for URLs.
- Return a plain username without `@`, preserving the first spelling and input order.
- Deduplicate case-insensitively across both accepted forms.
- Reject private/invite/post links, `tg://` links, bare usernames, malformed handles, queries, fragments, and existing invalid URL forms.
- Do not change dependencies, CLI arguments, Telegram collection behavior, or model calls.
- Do not edit or stage the user's current changes in `.env.example` or `DIGEST.md`.
- Follow RED-GREEN-REFACTOR and commit only explicitly named files.

---

### Task 1: Accept and document `@username` sources

**Files:**
- Modify: `tests/test_main.py:316-370`
- Modify: `main.py:132-176`
- Modify: `README.md:42-45`

**Interfaces:**
- Consumes: `USERNAME_RE`, `ConfigError`, and the existing `parse_channel_urls(text: str) -> list[str]` interface.
- Produces: the same `parse_channel_urls(text: str) -> list[str]` interface, now accepting both source forms and returning plain usernames for `load_channel_usernames(root: Path) -> list[str]`.

- [ ] **Step 1: Add focused failing parser and loader tests**

Add these methods to `ChannelParsingTests` in `tests/test_main.py`:

```python
def test_accepts_at_handles_and_deduplicates_across_source_forms(self):
    usernames = parse_channel_urls(
        "# sources\n@News\nhttps://t.me/news\nhttps://t.me/Second\n@SECOND\n"
    )

    self.assertEqual(usernames, ["News", "Second"])

def test_rejects_malformed_at_handles_with_their_line_number(self):
    for value in ("@", "@@news", "@news extra", "@news/42", "news"):
        with self.subTest(value=value):
            with self.assertRaisesRegex(ConfigError, r"Invalid channel source on line 2"):
                parse_channel_urls(f"@Valid\n{value}\n")
```

Change `LocalInputTests.test_loads_stripped_prompt_and_channels_from_given_root`
so its `DIGEST.md` fixture uses the handle form:

```python
(self.root / "DIGEST.md").write_text("# channels\n@News\n", encoding="utf-8")
```

Update `ChannelParsingTests.test_rejects_a_malformed_url_with_its_line_number`
to assert the source-oriented message:

```python
with self.assertRaisesRegex(ConfigError, r"Invalid channel source on line 2"):
    parse_channel_urls("https://t.me/News\nhttps://[::1\n")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_main.ChannelParsingTests.test_accepts_at_handles_and_deduplicates_across_source_forms \
  tests.test_main.ChannelParsingTests.test_rejects_malformed_at_handles_with_their_line_number \
  tests.test_main.ChannelParsingTests.test_rejects_a_malformed_url_with_its_line_number \
  tests.test_main.LocalInputTests.test_loads_stripped_prompt_and_channels_from_given_root \
  -v
```

Expected: FAIL because the current parser rejects `@News` and still reports
“Invalid channel URL” instead of “Invalid channel source”. Confirm the failure
comes from the missing behavior, not from a test syntax or import error.

- [ ] **Step 3: Implement the minimal two-branch parser**

Replace the per-line parsing portion of `parse_channel_urls` in `main.py` with
an explicit handle branch and the existing URL validation branch:

```python
        if line.startswith("@"):
            username = line[1:]
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
```

Keep the existing normalization and append logic unchanged:

```python
        normalized = username.lower()
        if normalized not in seen:
            seen.add(normalized)
            usernames.append(username)
```

Change the empty-source error in `load_channel_usernames` to describe both
accepted forms:

```python
raise ConfigError("DIGEST.md has no channel sources")
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the same focused command from Step 2.

Expected: all four focused tests PASS, including cross-format deduplication,
malformed handle rejection, malformed URL diagnostics, and local file loading.

- [ ] **Step 5: Run the complete regression suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: every test reports `ok`, the summary reports `OK`, and the command
exits with status 0.

- [ ] **Step 6: Document both accepted forms**

Replace the source-format paragraph in `README.md` with:

```markdown
В `DIGEST.md` укажите по одному публичному каналу в каждой строке: короткое
имя `@name` или корневой URL `https://t.me/name`. Пустые строки и строки,
начинающиеся с `#`, игнорируются. Одинаковые каналы удаляются с сохранением
порядка. `PROMPT.md` содержит системную инструкцию для генерации; по умолчанию
она просит русский дайджест с фактами, датами и ссылками на источники.
Форматы обоих файлов — обычный UTF-8 Markdown.
```

- [ ] **Step 7: Verify the final tree and user-change isolation**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile main.py
.venv/bin/python main.py --help
git diff --check
git status --short
```

Expected: 39 tests pass after adding two tests, compilation and `--help`
succeed, `git diff --check` is silent, and `git status --short` shows intended
changes to `main.py`, `tests/test_main.py`, and `README.md` plus the user's
pre-existing modifications to `.env.example` and `DIGEST.md`.

- [ ] **Step 8: Commit only the implementation files**

```bash
git add main.py tests/test_main.py README.md
git diff --cached --name-only
git commit -m "feat: accept @ channel sources"
```

Expected staged paths before the commit:

```text
README.md
main.py
tests/test_main.py
```

Do not stage `.env.example` or `DIGEST.md`.
