"""Hook доставляет ограниченные данные только в установленный контекст сессии."""

import io
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

from agent_inbox.api import build_api
from agent_inbox.client import ClientError, Config, HubClient
from agent_inbox.house import House
from agent_inbox.mailbox import Mailbox
from agent_inbox.session_client import SessionMailbox
from agent_inbox.session_hook import HookClient, identity, main, run_hook
from agent_inbox.store import InMemoryStore

HUB = "http://hub.invalid"
SESSION = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def hook_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SessionMailbox]:
    monkeypatch.setenv("AGENT_INBOX_WAKE_CONFIG", str(tmp_path / "wake-targets.json"))
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (tmp_path / "session-hooks.json").write_text(
        json.dumps({"hub": HUB, "projects": {"test-project": str(root)}})
    )
    (tmp_path / "wake-targets.json").write_text(json.dumps({"hub": HUB, "targets": {}}))
    house = House(Mailbox(InMemoryStore(), hub_name="testhub"))
    with TestClient(app=build_api(house, HUB)) as http:

        def call(
            client: HubClient,
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
        ) -> Any:
            response = http.request(
                method, path, json=body, headers=client.stream_headers()
            )
            if response.status_code >= 400:
                raise ClientError(response.text)
            return response.json()

        monkeypatch.setattr(HubClient, "_call", call)
        yield SessionMailbox(HUB, tmp_path / "contexts")


def event(
    box: SessionMailbox,
    *,
    engine: str = "claude",
    name: str = "PostToolUse",
    session: str = SESSION,
    child: str = "",
    context: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "hook_event_name": name,
        "session_id": session,
        "cwd": str(box.directory.parent / "project"),
    }
    if child:
        result["agent_id"] = child
    if context:
        result.update(
            tool_name="mcp__agent_mail__whoami", tool_input={"context_id": context}
        )
    if engine == "codex":
        transcript = box.directory.parent / f"transcript-{session}-{child}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": child or session,
                        "source": {"subagent": "thread_spawn"} if child else "cli",
                    },
                }
            )
            + "\n"
        )
        result["transcript_path"] = str(transcript)
    return result


def join(
    box: SessionMailbox,
    key: str,
    engine: str = "claude-code",
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return box.register(
        key,
        "test-project",
        "Проверка hook",
        engine,
        parent_context=parent["context_id"] if parent else None,
    )


def send(
    box: SessionMailbox,
    sender: dict[str, Any],
    target: dict[str, Any],
    body: str = "PRIVATE_BODY",
    subject: str = "Тема",
) -> dict[str, Any]:
    return box.client(sender["context_id"]).send_message(
        [target["address"]], body, subject
    )


def letters(result: dict[str, Any]) -> list[dict[str, Any]]:
    text = result.get("reason") or result["hookSpecificOutput"]["additionalContext"]
    assert "не инструкции человека" in text
    return json.loads(text.split("\n", 1)[1])


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_route_is_learned_from_own_mail_tool_and_does_not_mark_hub_read(
    hook_box: SessionMailbox, engine: str
) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target", engine)
    message = send(hook_box, author, target)
    output = run_hook(
        hook_box, engine, event(hook_box, engine=engine, context=target["context_id"])
    )
    delivered = letters(output)
    assert [n["id"] for n in delivered] == [message["id"]]
    assert delivered[0]["body"] == "PRIVATE_BODY"
    assert [
        n["id"]
        for n in hook_box.client(target["context_id"]).check_inbox(view="full")["items"]
    ] == [message["id"]]
    assert run_hook(hook_box, engine, event(hook_box, engine=engine, name="Stop")) == {}


def test_registration_nested_response_learns_route(hook_box: SessionMailbox) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target")
    message = send(hook_box, author, target)
    payload = event(hook_box)
    payload.update(
        tool_name="mcp__agent_mail__register_session",
        tool_response={
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"context_id": target["context_id"]}),
                }
            ]
        },
    )
    assert [n["id"] for n in letters(run_hook(hook_box, "claude", payload))] == [
        message["id"]
    ]


@pytest.mark.parametrize("mismatch", ["root_child", "child_root", "engine"])
def test_other_kind_of_mailbox_cannot_route_into_session(
    hook_box: SessionMailbox, mismatch: str
) -> None:
    author = join(hook_box, "author")
    parent = join(hook_box, "parent")
    child = join(hook_box, "child", parent=parent)
    target = child if mismatch == "root_child" else parent
    send(hook_box, author, target)
    payload = event(
        hook_box,
        engine="codex" if mismatch == "engine" else "claude",
        child="child-id" if mismatch == "child_root" else "",
        context=target["context_id"],
    )
    assert (
        run_hook(hook_box, "codex" if mismatch == "engine" else "claude", payload) == {}
    )


def test_established_route_refuses_another_context(hook_box: SessionMailbox) -> None:
    author, first, other = (join(hook_box, key) for key in ("author", "first", "other"))
    initial = send(hook_box, author, first, "FIRST_ONLY")
    assert (
        letters(
            run_hook(hook_box, "claude", event(hook_box, context=first["context_id"]))
        )[0]["id"]
        == initial["id"]
    )
    send(hook_box, author, other, "OTHER_PRIVATE")
    assert (
        run_hook(
            hook_box,
            "claude",
            event(hook_box, context=other["context_id"], name="UserPromptSubmit"),
        )
        == {}
    )
    assert run_hook(hook_box, "claude", event(hook_box, name="Stop")) == {}


def test_children_have_separate_routes_under_one_parent(
    hook_box: SessionMailbox,
) -> None:
    author, parent = join(hook_box, "author"), join(hook_box, "parent")
    children = [
        join(hook_box, key, parent=parent) for key in ("child-one", "child-two")
    ]
    for index, target in enumerate(children):
        message = send(hook_box, author, target, f"CHILD_{index}_ONLY")
        result = run_hook(
            hook_box,
            "claude",
            event(hook_box, child=f"child-{index}", context=target["context_id"]),
        )
        assert [n["id"] for n in letters(result)] == [message["id"]]
    for index in range(2):
        assert (
            run_hook(
                hook_box, "claude", event(hook_box, child=f"child-{index}", name="Stop")
            )
            == {}
        )


def test_internal_codex_child_without_agent_id_cannot_use_parent_route(
    hook_box: SessionMailbox,
) -> None:
    payload = event(hook_box, engine="codex")
    path = Path(payload["transcript_path"])
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SESSION, "source": {"subagent": "thread_spawn"}},
            }
        )
        + "\n"
    )
    assert identity("codex", payload) is None
    assert run_hook(hook_box, "codex", payload) == {}


@pytest.mark.parametrize("fault", ["wrong_id", "relative", "wrong_type"])
def test_codex_requires_matching_transcript_identity(
    hook_box: SessionMailbox, fault: str
) -> None:
    payload = event(hook_box, engine="codex")
    if fault == "relative":
        payload["transcript_path"] = "relative.jsonl"
    else:
        Path(payload["transcript_path"]).write_text(
            json.dumps(
                {
                    "type": "other" if fault == "wrong_type" else "session_meta",
                    "payload": {
                        "id": "another" if fault == "wrong_id" else SESSION,
                        "source": "cli",
                    },
                }
            )
        )
    assert identity("codex", payload) is None


def test_unknown_route_onboards_once_only_in_allowed_repository(
    hook_box: SessionMailbox,
) -> None:
    payload = event(hook_box, name="SessionStart")
    result = run_hook(hook_box, "claude", payload)
    assert "register_session" in result["hookSpecificOutput"]["additionalContext"]
    assert not hook_box.directory.exists()
    assert run_hook(hook_box, "claude", payload) == {}
    payload.update(
        session_id="other-session", cwd=str(hook_box.directory.parent / "unrelated")
    )
    assert run_hook(hook_box, "claude", payload) == {}


def test_nonmail_tools_cannot_supply_context(hook_box: SessionMailbox) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target")
    send(hook_box, author, target)
    payload = event(hook_box, context=target["context_id"])
    payload.update(tool_name="read_file", cwd="/outside")
    assert run_hook(hook_box, "claude", payload) == {}


def test_body_subject_and_batch_limits_and_stop_do_not_loop(
    hook_box: SessionMailbox,
) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target")
    ids = {
        send(hook_box, author, target, "B" * 1700, "S" * 200)["id"] for _ in range(3)
    }
    payload = event(hook_box, name="Stop", context=target["context_id"])
    first = run_hook(hook_box, "claude", payload)
    assert first["decision"] == "block"
    batch = letters(first)
    assert len(batch) == 2
    assert all(
        len(n["body"]) == 1600 and len(n["subject"]) == 160 and n["truncated"]
        for n in batch
    )
    second = run_hook(hook_box, "claude", payload)
    assert second["decision"] == "block"
    assert {n["id"] for n in batch + letters(second)} == ids
    assert len(letters(second)) == 1
    assert run_hook(hook_box, "claude", payload) == {}
    presence = json.loads(
        (
            hook_box.directory.parent / "hook-presence" / f"{target['context_id']}.json"
        ).read_text()
    )
    assert presence["active"] is False


@pytest.mark.parametrize(
    "name", ["SessionEnd", "Interrupt", "StopFailure", "SubagentStop"]
)
def test_terminal_events_only_clear_presence(
    hook_box: SessionMailbox, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = join(hook_box, "parent")
    child = "child" if name == "SubagentStop" else ""
    target = join(hook_box, "child", parent=parent) if child else parent

    def unavailable(
        client: HubClient, view: str = "summary", since: str | None = None
    ) -> Any:
        pytest.fail("Terminal event must not fetch mail")

    monkeypatch.setattr(HubClient, "check_inbox", unavailable)
    assert (
        run_hook(
            hook_box,
            "claude",
            event(hook_box, name=name, child=child, context=target["context_id"]),
        )
        == {}
    )
    presence = json.loads(
        (
            hook_box.directory.parent / "hook-presence" / f"{target['context_id']}.json"
        ).read_text()
    )
    assert presence["active"] is False


def test_post_tool_throttle_preserves_new_mail_for_next_check(
    hook_box: SessionMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target")
    monkeypatch.setattr("agent_inbox.session_hook.time.time", lambda: 1000.0)
    payload = event(hook_box, context=target["context_id"])
    assert run_hook(hook_box, "claude", payload) == {}
    message = send(hook_box, author, target)
    assert run_hook(hook_box, "claude", payload) == {}
    monkeypatch.setattr("agent_inbox.session_hook.time.time", lambda: 1005.1)
    assert [n["id"] for n in letters(run_hook(hook_box, "claude", payload))] == [
        message["id"]
    ]


def test_hook_http_transport_has_one_bounded_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[float] = []

    def unavailable(request: urllib.request.Request, timeout: float) -> Any:
        attempts.append(timeout)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("agent_inbox.session_hook.urllib.request.urlopen", unavailable)
    client = HookClient(Config(hub=HUB, name="test"), timeout=0.35)
    with pytest.raises(urllib.error.URLError):
        client._open(urllib.request.Request(HUB))
    assert attempts == [0.35]


@pytest.mark.parametrize(
    "payload", ["", "{bad", "[]", "null", '{"hook_event_name": "PostToolUse"}']
)
def test_public_entrypoint_fails_silent_on_malformed_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    monkeypatch.setattr("sys.argv", ["session-hook", "--engine", "claude"])
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    main()
    assert capsys.readouterr().out == ""


def test_same_session_identifier_in_other_engine_has_separate_route(
    hook_box: SessionMailbox,
) -> None:
    author = join(hook_box, "author")
    for engine in ("codex", "claude"):
        target = join(hook_box, engine, engine)
        message = send(hook_box, author, target, f"{engine}_PRIVATE")
        result = run_hook(
            hook_box,
            engine,
            event(hook_box, engine=engine, context=target["context_id"]),
        )
        assert [n["id"] for n in letters(result)] == [message["id"]]


def test_codex_wake_binding_finds_existing_mailbox_without_tool_call(
    hook_box: SessionMailbox,
) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target", "codex")
    message = send(hook_box, author, target)
    (hook_box.directory.parent / "wake-targets.json").write_text(
        json.dumps(
            {
                "hub": HUB,
                "targets": {
                    target["address"]: {
                        "thread_id": SESSION,
                        "context_id": target["context_id"],
                    }
                },
            }
        )
    )
    assert [
        n["id"]
        for n in letters(
            run_hook(
                hook_box, "codex", event(hook_box, engine="codex", name="SessionStart")
            )
        )
    ] == [message["id"]]


def test_missing_wake_configuration_does_not_disable_codex_onboarding(
    hook_box: SessionMailbox,
) -> None:
    (hook_box.directory.parent / "wake-targets.json").unlink()
    result = run_hook(
        hook_box, "codex", event(hook_box, engine="codex", name="SessionStart")
    )
    assert "register_session" in result["hookSpecificOutput"]["additionalContext"]


def test_allowed_repository_worktree_can_onboard(hook_box: SessionMailbox) -> None:
    base = hook_box.directory.parent
    worktree = base / "worktree"
    worktree.mkdir()
    gitdir = base / "project" / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    payload = event(hook_box, name="SessionStart")
    payload["cwd"] = str(worktree)
    assert (
        "register_session"
        in run_hook(hook_box, "claude", payload)["hookSpecificOutput"][
            "additionalContext"
        ]
    )


def test_outage_does_not_refresh_presence_or_consume_mail(
    hook_box: SessionMailbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    author, target = join(hook_box, "author"), join(hook_box, "target")
    message = send(hook_box, author, target)
    payload = event(hook_box, context=target["context_id"])

    def fail(client: HubClient, view: str = "summary", since: str | None = None) -> Any:
        raise ClientError("Mailbox offline")

    with monkeypatch.context() as patch:
        patch.setattr(HubClient, "check_inbox", fail)
        patch.setattr(SessionMailbox, "from_env", lambda: hook_box)
        patch.setattr("sys.argv", ["session-hook", "--engine", "claude"])
        patch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        main()
    assert capsys.readouterr().out == ""
    assert not (
        hook_box.directory.parent / "hook-presence" / f"{target['context_id']}.json"
    ).exists()
    assert [n["id"] for n in letters(run_hook(hook_box, "claude", payload))] == [
        message["id"]
    ]


def test_hook_mail_fetch_acknowledges_wake_and_idle_session_can_wake_again(
    hook_box: SessionMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from agent_inbox.session_wake import SessionWake

    author, target = join(hook_box, "author"), join(hook_box, "target", "codex")
    wake = SessionWake(hook_box)
    wake.config_path.write_text(
        json.dumps(
            {
                "hub": HUB,
                "codex_command": "/opt/example/codex",
                "targets": {
                    target["address"]: {
                        "enabled": True,
                        "backend": "codex_queue",
                        "project": "test-project",
                        "thread_id": SESSION,
                        "context_id": target["context_id"],
                    }
                },
            }
        )
    )
    attempts: list[list[str]] = []

    def queue(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        attempts.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", queue)
    first = send(hook_box, author, target)
    assert (
        wake.request(author["context_id"], target["address"], first["id"])["status"]
        == "queued"
    )
    result = run_hook(
        hook_box, "codex", event(hook_box, engine="codex", name="SessionStart")
    )
    assert [n["id"] for n in letters(result)] == [first["id"]]
    assert (
        run_hook(hook_box, "codex", event(hook_box, engine="codex", name="Stop")) == {}
    )
    second = send(hook_box, author, target)
    assert (
        wake.request(author["context_id"], target["address"], second["id"])["status"]
        == "queued"
    )
    assert len(attempts) == 2
