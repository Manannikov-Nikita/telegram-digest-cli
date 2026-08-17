# Support `@username` Channel Sources

## Goal

Allow each non-comment line in `DIGEST.md` to identify a public Telegram
channel as either `@username` or `https://t.me/username`. Existing URL behavior
must remain compatible.

## Parsing contract

- Ignore empty lines and lines whose first non-whitespace character is `#`.
- Accept exactly two source forms:
  - `@username`
  - `https://t.me/username`, with the existing optional trailing slash
- Validate the extracted username with the existing `USERNAME_RE` rule.
- Reject empty handles, doubled `@`, whitespace or prose after a handle,
  invites, post links, query strings, fragments, and all previously rejected
  URL forms.
- Report the failing `DIGEST.md` line number as an invalid channel source.

## Normalization and data flow

The parser returns the plain username without `@`. It deduplicates sources
case-insensitively across both forms while preserving the first occurrence's
spelling and input order. The collection layer therefore remains unchanged:
Telethon continues to receive a plain username and still verifies that the
resolved entity is a public broadcast channel.

## Code scope

Extend the existing source parser in `main.py` with an explicit branch for
`@username`; keep the strict URL branch. Update user-facing configuration
errors from “channel URL” to “channel source” where they describe both forms.
Document both forms in `README.md`. No dependencies, CLI arguments, Telegram
collection behavior, or model calls change.

## Tests

Add standard `unittest` coverage that proves:

- `@username` is accepted and returned without `@`;
- an `@username` and equivalent URL deduplicate case-insensitively;
- the first spelling and order are preserved;
- malformed handle forms fail with the correct line number;
- existing valid URLs and invalid URL cases retain their behavior;
- loading `DIGEST.md` accepts a handle source.

Run the complete suite after the focused red-green cycle.

## Out of scope

Private channels, invite links, post links, `tg://` links, bare usernames,
automatic joining, and entity-type inference during local parsing remain out
of scope.
