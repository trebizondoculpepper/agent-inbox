"""Пробуждение разрешённого чата не подменяет доставку и полномочия отправителя."""

import hashlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from litestar.testing import TestClient

from agent_inbox.api import build_api
from agent_inbox.client import ClientError, HubClient
from agent_inbox.house import House
from agent_inbox.mailbox import Mailbox
from agent_inbox.session_client import SessionMailbox
from agent_inbox.session_tools import build_session_server
from agent_inbox.session_wake import SessionWake
from agent_inbox.store import InMemoryStore

HUB = "http://hub.invalid"
THREAD = "12345678-1234-1234-1234-123456789abc"
COMMAND = "/opt/example/codex"


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionMailbox]:
    monkeypatch.setenv("AGENT_INBOX_WAKE_CONFIG", str(tmp_path / "wake-targets.json"))
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


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 15,
            "check": False,
        }
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", run)
    return calls


def join(
    box: SessionMailbox, key: str, project: str = "test-project"
) -> dict[str, Any]:
    return box.register(key, project, "Проверка пробуждения", "codex")


def allow(box: SessionMailbox, *records: dict[str, Any]) -> dict[str, Any]:
    config = {
        "hub": HUB,
        "codex_command": COMMAND,
        "targets": {
            r["address"]: {
                "enabled": True,
                "backend": "codex_queue",
                "project": r["project"],
                "context_id": r["context_id"],
                "thread_id": THREAD,
            }
            for r in records
        },
    }
    SessionWake(box).config_path.write_text(json.dumps(config))
    return config


def send(
    box: SessionMailbox, author: dict[str, Any], *recipients: dict[str, Any]
) -> dict[str, Any]:
    return box.client(author["context_id"]).send_message(
        [r["address"] for r in recipients],
        "PEER_BODY --dangerously-bypass-approvals-and-sandbox",
        "PEER_SUBJECT",
    )


def state_file(box: SessionMailbox, recipient: dict[str, Any]) -> Path:
    key = hashlib.sha256(f"{HUB}\n{recipient['address']}".encode()).hexdigest()
    directory = box.directory.parent / "wake-state"
    directory.mkdir(exist_ok=True)
    return directory / f"{key}.json"


def test_queue_uses_only_local_binding_and_static_notice(
    box: SessionMailbox, queued: list[list[str]]
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    message = send(box, author, target)
    result = SessionWake(box).request(
        author["context_id"], target["address"], message["id"]
    )
    assert result["status"] == "queued"
    assert len(queued) == 1
    command = queued[0]
    assert command[:5] == [COMMAND, "queue", "--thread", THREAD, "--message"]
    assert len(command) == 6
    notice = command[5]
    assert target["context_id"] in notice
    assert message["id"].rsplit("/", 1)[-1] in notice
    assert "PEER_BODY" not in notice and "PEER_SUBJECT" not in notice
    assert "dangerously" not in notice
    assert "не новый запрос человека" in notice
    assert "GO" in notice
    assert "agent-inbox session call check_inbox" in notice
    assert "agent-post" not in notice and "agent_mail" not in notice


@pytest.mark.parametrize("reason", ["author", "recipient", "project", "self", "group"])
def test_request_rejects_unrelated_or_broad_wakes(
    box: SessionMailbox, queued: list[list[str]], reason: str
) -> None:
    author, target, other = join(box, "author"), join(box, "target"), join(box, "other")
    allow(box, author, target, other)
    message = send(box, author, target)
    context, address = author["context_id"], target["address"]
    if reason == "author":
        message = send(box, author, target, other)
        context = target["context_id"]
        address = other["address"]
    elif reason == "recipient":
        address = other["address"]
    elif reason == "project":
        alien = join(box, "alien", "other-project")
        allow(box, alien)
        message = send(box, author, alien)
        address = alien["address"]
    elif reason == "self":
        message = send(box, author, author)
        address = author["address"]
    else:
        address = "project:test-project"
    with pytest.raises(ClientError):
        SessionWake(box).request(context, address, message["id"])
    assert queued == []


@pytest.mark.parametrize(
    "fault",
    [
        "hub",
        "thread_id",
        "context_id",
        "enabled",
        "backend",
        "project",
        "command",
        "null_command",
        "root",
        "targets",
    ],
)
def test_malformed_or_disallowed_binding_fails_closed(
    box: SessionMailbox, queued: list[list[str]], fault: str
) -> None:
    author, target = join(box, "author"), join(box, "target")
    config: Any = allow(box, target)
    binding = config["targets"][target["address"]]
    if fault == "hub":
        config["hub"] = "http://other.invalid"
    elif fault == "command":
        config["codex_command"] = "codex"
    elif fault == "null_command":
        config["codex_command"] = "/opt/co\x00dex"
    elif fault == "root":
        config = []
    elif fault == "targets":
        config["targets"] = []
    else:
        binding[fault] = {
            "thread_id": "--evil",
            "context_id": author["context_id"],
            "enabled": "true",
            "backend": "shell",
            "project": "other-project",
        }[fault]
    SessionWake(box).config_path.write_text(json.dumps(config))
    assert SessionWake(box).capability(target["address"], target["project"]) == {
        "supported": False
    }
    message = send(box, author, target)
    with pytest.raises(ClientError):
        SessionWake(box).request(author["context_id"], target["address"], message["id"])
    assert queued == []


def test_duplicate_is_per_message_and_recipient_and_survives_restart(
    box: SessionMailbox, queued: list[list[str]]
) -> None:
    author, first, second = (join(box, key) for key in ("author", "first", "second"))
    allow(box, first, second)
    message = send(box, author, first, second)
    for target in (first, second):
        result = SessionWake(box).request(
            author["context_id"], target["address"], message["id"]
        )
        assert result["status"] == "queued"
        duplicate = SessionWake(SessionMailbox(HUB, box.directory)).request(
            author["context_id"], target["address"], message["id"]
        )
        assert duplicate["duplicate"] is True
    assert len(queued) == 2


def test_six_per_minute_and_later_retry_after_rate_limit(
    box: SessionMailbox, queued: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    monkeypatch.setattr("agent_inbox.session_wake.time.time", lambda: 1000.0)
    messages = [send(box, author, target) for _ in range(7)]
    outcomes = [
        SessionWake(box).request(
            author["context_id"], target["address"], message["id"]
        )["status"]
        for message in messages
    ]
    assert outcomes == ["queued"] * 6 + ["rate_limited"]
    assert len(queued) == 6
    monkeypatch.setattr("agent_inbox.session_wake.time.time", lambda: 1061.0)
    result = SessionWake(box).request(
        author["context_id"], target["address"], messages[-1]["id"]
    )
    assert result["status"] == "queued"
    assert len(queued) == 7


def test_timeout_is_unknown_and_never_queued_again(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    message = send(box, author, target)
    calls = []

    def timeout(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", timeout)
    first = SessionWake(box).request(
        author["context_id"], target["address"], message["id"]
    )
    assert first["status"] == "unknown"
    duplicate = SessionWake(box).request(
        author["context_id"], target["address"], message["id"]
    )
    assert duplicate["status"] == "unknown" and duplicate["duplicate"] is True
    assert len(calls) == 1


async def test_default_does_not_wake_and_failure_keeps_delivery(
    box: SessionMailbox, queued: list[list[str]]
) -> None:
    author, target = join(box, "author"), join(box, "target")
    async with Client(build_session_server(box)) as mcp:
        args = {
            "context_id": author["context_id"],
            "to": [target["address"]],
            "body": "Доставить",
        }
        plain = (await mcp.call_tool("send_message", args)).data
        assert "wake_requests" not in plain
        failed = (await mcp.call_tool("send_message", {**args, "wake": True})).data
        assert failed["wake_requests"][0]["status"] == "failed"
    inbox = box.client(target["context_id"]).check_inbox(view="full")["items"]
    assert {n["id"] for n in inbox} == {plain["id"], failed["id"]}
    assert queued == []


@pytest.mark.parametrize(
    "bad",
    [
        {"requests": []},
        {"requests": {"other": None}},
        {"requests": {"other": {}}},
        {"requests": {"other": {"at": "yesterday"}}},
        {"requests": {"other": {"at": float("nan")}}},
    ],
)
async def test_corrupt_ledger_reports_wake_failure_without_hiding_delivery(
    box: SessionMailbox, queued: list[list[str]], bad: dict[str, Any]
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    state_file(box, target).write_text(json.dumps(bad))
    async with Client(build_session_server(box)) as mcp:
        result = (
            await mcp.call_tool(
                "send_message",
                {
                    "context_id": author["context_id"],
                    "to": [target["address"]],
                    "body": "Сохранить письмо",
                    "wake": True,
                },
            )
        ).data
    assert result["wake_requests"][0]["status"] == "failed"
    assert queued == []
    assert [
        n["id"]
        for n in box.client(target["context_id"]).check_inbox(view="full")["items"]
    ] == [result["id"]]


@pytest.mark.parametrize("recipient", ["self", "everyone", "project:test-project"])
async def test_send_with_wake_rejects_self_and_group_before_delivery(
    box: SessionMailbox, queued: list[list[str]], recipient: str
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, author, target)
    address = author["address"] if recipient == "self" else recipient
    async with Client(build_session_server(box)) as mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "send_message",
                {
                    "context_id": author["context_id"],
                    "to": [address],
                    "body": "Не доставлять",
                    "wake": True,
                },
            )
    assert box.client(author["context_id"]).check_inbox(view="full")["items"] == []
    assert box.client(target["context_id"]).check_inbox(view="full")["items"] == []
    assert queued == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("at", float("nan")),
        ("at", "yesterday"),
        ("at", True),
        ("status", []),
        ("status", {}),
    ],
)
async def test_malformed_field_in_otherwise_valid_ledger_is_a_delivery_preserving_error(
    box: SessionMailbox, queued: list[list[str]], field: str, value: Any
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    message = send(box, author, target)
    saved_id = message["id"].rsplit("/", 1)[-1]
    entry = {
        "recipient": target["address"],
        "message_id": message["id"],
        "status": "queued",
        "at": 1.0,
        field: value,
    }
    state_file(box, target).write_text(json.dumps({"requests": {saved_id: entry}}))
    async with Client(build_session_server(box)) as mcp:
        result = (
            await mcp.call_tool(
                "send_message",
                {
                    "context_id": author["context_id"],
                    "to": [target["address"]],
                    "body": "Ещё одно сохранённое письмо",
                    "wake": True,
                },
            )
        ).data
    assert result["wake_requests"][0]["status"] == "failed"
    ids = {
        n["id"]
        for n in box.client(target["context_id"]).check_inbox(view="full")["items"]
    }
    assert ids == {message["id"], result["id"]}
    assert queued == []


def test_cc_recipient_may_be_woken(
    box: SessionMailbox, queued: list[list[str]]
) -> None:
    author, target = join(box, "author"), join(box, "target")
    allow(box, target)
    client = box.client(author["context_id"])
    message = client._call(
        "POST",
        f"/actors/{author['address']}/outbox",
        {
            "type": "Note",
            "to": [],
            "cc": [target["address"]],
            "content": "Копия письма",
        },
    )
    assert message["cc"] == [f"{HUB}/actors/{target['address']}"]
    result = SessionWake(box).request(
        author["context_id"], target["address"], message["id"]
    )
    assert result["status"] == "queued"
    assert len(queued) == 1
