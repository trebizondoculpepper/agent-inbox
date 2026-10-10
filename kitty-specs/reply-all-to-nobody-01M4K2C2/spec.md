# Feature Specification: Reply-all to nobody is refused

**Mission**: `reply-all-to-nobody-01M4K2C2`
**Created**: 2026-10-10
**Status**: In review (pull request)
**Source**: [GitHub issue #88](https://github.com/trebizondoculpepper/agent-inbox/issues/88), raised in review of #79.

## Purpose

`rules.reply_recipients` returns the original's author plus its `to` and `cc`, minus the
caller. When the caller was the only party to the original — a note to self, answered
with reply-all — the result is empty. `Mailbox.send` refuses an audience that resolves to
nobody, but only when something was addressed, so an empty list passed straight through:
the reply was stored, addressed to no one, and the API answered **201 Created**. The
sender is told the reply went; nothing arrives anywhere. That is the "check that passed
with nothing to look at" shape the charter warns about.

## Acceptance scenarios

1. **Given** a note whose only party is the caller, **when** the caller replies to all,
   **then** the hub answers `422 delivers_to_nobody` and stores nothing.
2. **Given** an ordinary reply-all with other parties, **then** behaviour is unchanged.
3. **Given** a send whose recipients are all remote, **then** behaviour is unchanged —
   `House` removes remote recipients before `Mailbox.send`, so an empty *local* `to` is
   legitimate there.

## Decisions

- **Refused in `rules.reply_recipients`, not in `Mailbox.send`.** Making `send` refuse an
  empty `to` was tried first and broke scenario 3. The rule is the one place both reply
  paths (`House.reply` and `Mailbox.reply`) share.
- **422 `delivers_to_nobody`, not the 400 the issue suggested.** The hub already has an
  error for "every name was real and none reaches anyone", and its clients already
  understand it; a second code for the same outcome would be a distinction without a
  remedy.

## Verification

`tests/test_session_collaboration.py::test_reply_all_with_nobody_left_is_refused_not_created`
drives the API. Removal proof run: with the guard disabled it fails (`DID NOT RAISE`);
restored, it and the existing reply-all tests pass.
