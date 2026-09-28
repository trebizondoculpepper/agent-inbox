"""The waiter's watermark and lock are per engine, not per project (#77).

Reported by jakekinchen with a four-line, network-free reproduction. Identities are per
engine — `[agents.claude]` and `[agents.codex]` in one `agent-inbox.toml` — but the
announce-once watermark and the waiter lock were kept once per project. Two engines in
one checkout each saved *their own* unread set over the other's, so after every Codex
turn Claude's unread mail looked new again and an idle Claude session was re-woken for
it: about nine wakes for the same five messages in one afternoon. The lock had the
matching fault: only one engine's waiter could hold the project at a time.

Found while checking the suggested fix: agent-inbox never added these files to a user's
`.gitignore` either — it wrote them into the project root and left them addable.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from agent_inbox import hookconfig, ignores, wake


def _unread(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"/objects/m{i}",
            "attributedTo": "/actors/someone",
            "summary": f"msg {i}",
        }
        for i in range(n)
    ]


def _turn(root: Path, engine: str, event: str, unread: list[dict[str, Any]]) -> int:
    r = wake.wake_response(event, unread, wake._load_seen(root, engine))
    wake._save_seen(root, r.seen, engine)
    return r.exit_code


class TestTwoEnginesDoNotReannounceEachOthersMail:
    def test_the_reported_reproduction_is_now_silent(self, tmp_path: Path) -> None:
        """The report's four lines. The last one was 2 — the same five again."""
        claude, codex = _unread(5), []

        assert _turn(tmp_path, "claude", "Stop", claude) == 2
        assert _turn(tmp_path, "claude", "Stop", claude) == 0
        assert _turn(tmp_path, "codex", "UserPromptSubmit", codex) == 0
        assert _turn(tmp_path, "claude", "Stop", claude) == 0

    def test_each_engine_keeps_its_own_file(self, tmp_path: Path) -> None:
        _turn(tmp_path, "claude", "Stop", _unread(2))
        _turn(tmp_path, "codex", "Stop", [])

        assert (tmp_path / ".agent-mailbox-seen.claude.json").exists()
        assert (tmp_path / ".agent-mailbox-seen.codex.json").exists()

    def test_run_once_keys_by_the_detected_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hooks pass no --engine on Claude Code; the marker in the environment is
        how the waiter knows whose state this is."""
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setattr(wake, "_fetch_unread", lambda root, engine=None: _unread(1))

        wake._run_once("Stop", tmp_path)

        assert (tmp_path / ".agent-mailbox-seen.claude.json").exists()
        assert not (tmp_path / wake.WATERMARK_NAME).exists()


class TestTheLockIsPerEngine:
    def test_two_engines_can_each_hold_a_waiter(self, tmp_path: Path) -> None:
        """The matching fault: one engine's waiter used to lock the other out."""
        with wake._single_waiter(tmp_path, max_age=60, engine="claude") as claude:
            with wake._single_waiter(tmp_path, max_age=60, engine="omp") as omp:
                assert claude and omp

    def test_one_engine_still_gets_one_waiter(self, tmp_path: Path) -> None:
        """The paired positive: per engine must not mean unlimited."""
        with wake._single_waiter(tmp_path, max_age=60, engine="claude") as first:
            with wake._single_waiter(tmp_path, max_age=60, engine="claude") as second:
                assert first and not second

    def test_the_waiter_takes_its_own_engines_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another engine's live lock no longer stops this engine's waiter."""
        (tmp_path / ".agent-mailbox-wake.omp.lock").write_text(
            json.dumps({"pid": os.getpid(), "created": time.time()})
        )
        monkeypatch.setattr(wake, "_fetch_unread", lambda root, engine=None: _unread(1))

        code = wake.run(
            "Stop",
            root=tmp_path,
            wait=True,
            engine="claude",
            poll_interval=0.1,
            wait_timeout=1.0,
            sleep=lambda s: None,
        )

        assert code == 2


class TestUpgradingDoesNotBurstAnnouncements:
    def test_the_legacy_watermark_seeds_an_engine_once(self, tmp_path: Path) -> None:
        """Without the seed, the first turn after upgrading re-announces everything —
        the very burst this issue is about. The legacy file is written by a real turn
        with no engine, as 1.7.0 wrote it, so the test does not encode its format."""
        assert _turn(tmp_path, None, "Stop", _unread(2)) == 2  # type: ignore[arg-type]
        assert not (tmp_path / ".agent-mailbox-seen.claude.json").exists()

        assert _turn(tmp_path, "claude", "Stop", _unread(2)) == 0

    def test_the_engines_own_file_wins_over_the_legacy_one(
        self, tmp_path: Path
    ) -> None:
        """Seeded once, never again: after the engine has its own file, what the legacy
        file says no longer matters."""
        _turn(tmp_path, None, "Stop", _unread(1))  # type: ignore[arg-type]
        wake._save_seen(tmp_path, frozenset(), "claude")

        assert _turn(tmp_path, "claude", "Stop", _unread(1)) == 2

    def test_no_engine_keeps_the_legacy_name(self, tmp_path: Path) -> None:
        """Unresolvable is not an error; it is the old behaviour, unchanged."""
        _turn(tmp_path, None, "Stop", _unread(1))  # type: ignore[arg-type]

        assert (tmp_path / wake.WATERMARK_NAME).exists()

    def test_an_unsafe_engine_name_never_becomes_a_path(self) -> None:
        assert wake._engine_for("../../etc") is None
        assert wake._engine_for("claude") == "claude"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,  # noqa: S607
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


class TestTheStateFilesStayOutOfGit:
    def test_installing_any_hook_ignores_the_state_files(self, repo: Path) -> None:
        hookconfig.install_for("opencode", repo)
        for name in (
            ".agent-mailbox-seen.json",
            ".agent-mailbox-seen.claude.json",
            ".agent-mailbox-wake.lock",
            ".agent-mailbox-wake.codex.lock",
        ):
            (repo / name).write_text("{}")
        _git(repo, "add", "-A")

        staged = _git(repo, "diff", "--cached", "--name-only")
        assert ".agent-mailbox" not in staged

    def test_reinstalling_adds_each_rule_once(self, repo: Path) -> None:
        hookconfig.install_for("opencode", repo)
        hookconfig.install_for("codex", repo)

        text = (repo / ".gitignore").read_text()
        for rule, _ in ignores.WAKE_STATE_RULES:
            assert text.count(rule) == 1

    def test_doctor_sees_a_committed_watermark(self, repo: Path) -> None:
        """An ignore rule cannot help a file git already tracks."""
        path = repo / ".agent-mailbox-seen.claude.json"
        path.write_text("{}")
        _git(repo, "add", "-f", path.name)
        _git(repo, "commit", "-qm", "oops")

        assert ignores.exposed_wake_state(repo) == [(path, "tracked")]

    def test_an_ignored_watermark_is_not_reported(self, repo: Path) -> None:
        hookconfig.install_for("opencode", repo)
        (repo / ".agent-mailbox-seen.claude.json").write_text("{}")

        assert ignores.exposed_wake_state(repo) == []
