"""Накопившаяся почта вызывает один короткий сигнал до успешного чтения inbox."""

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from litestar.testing import TestClient

from agent_inbox.api import build_api
from agent_inbox.client import ClientError, HubClient
from agent_inbox.house import House
from agent_inbox.locking import LockUnavailable
from agent_inbox.mailbox import Mailbox
from agent_inbox.session_client import SessionMailbox
from agent_inbox.session_tools import build_session_server
from agent_inbox.session_wake import SessionWake
from agent_inbox.store import InMemoryStore

HUB = "http://hub.invalid"


@pytest.fixture
def quiet_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SessionMailbox]:
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
def launches(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", run)
    return calls


def participants(box: SessionMailbox) -> tuple[dict[str, Any], dict[str, Any]]:
    author = box.register("sender", "test-project", "Отправитель", "codex")
    target = box.register("receiver", "test-project", "Получатель", "codex")
    box.status(target["context_id"], "completed")
    SessionWake(box).config_path.write_text(
        json.dumps(
            {
                "hub": HUB,
                "codex_command": "/opt/example/codex",
                "targets": {
                    target["address"]: {
                        "enabled": True,
                        "backend": "codex_queue",
                        "project": target["project"],
                        "context_id": target["context_id"],
                        "thread_id": "12345678-1234-1234-1234-123456789abc",
                    }
                },
            }
        )
    )
    return author, target


def deliver(
    box: SessionMailbox, author: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    return box.client(author["context_id"]).send_message(
        [target["address"]], "PRIVATE_PEER_BODY", "PRIVATE_PEER_SUBJECT"
    )


def request(
    box: SessionMailbox,
    author: dict[str, Any],
    target: dict[str, Any],
    message: dict[str, Any],
) -> dict[str, Any]:
    return SessionWake(box).request(
        author["context_id"], target["address"], message["id"]
    )


def test_completed_recipient_gets_one_short_notice_without_peer_content(
    quiet_box: SessionMailbox, launches: list[list[str]]
) -> None:
    author, target = participants(quiet_box)
    message = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, message)["status"] == "queued"
    assert len(launches) == 1
    notice = launches[0][-1]
    assert len(notice) < 260
    assert target["context_id"] in notice
    assert message["id"].rsplit("/", 1)[-1] not in notice
    assert "PRIVATE_PEER" not in notice
    assert "inbox" in notice
    assert "не новый запрос человека" in notice and "не новый GO" in notice


@pytest.mark.parametrize("status", ["active", "waiting", None])
def test_advisory_status_does_not_suppress_explicit_wake(
    quiet_box: SessionMailbox, launches: list[list[str]], status: str | None
) -> None:
    author, target = participants(quiet_box)
    client = quiet_box.client(target["context_id"])
    profile = client.whois(target["address"])["profile"]
    if status is None:
        profile.pop("status")
    else:
        profile["status"] = status
    client.update_profile(profile)
    actual = client.whois(target["address"])["profile"]
    assert actual.get("status") == status
    message = deliver(quiet_box, author, target)
    result = request(quiet_box, author, target, message)
    assert result["status"] == "queued"
    assert len(launches) == 1
    assert [item["id"] for item in client.check_inbox(view="full")["items"]] == [
        message["id"]
    ]


async def test_different_messages_coalesce_across_clients_until_acknowledged(
    quiet_box: SessionMailbox, launches: list[list[str]]
) -> None:
    author, target = participants(quiet_box)
    first, second = (deliver(quiet_box, author, target) for _ in range(2))
    assert first["id"] != second["id"]
    assert request(quiet_box, author, target, first)["status"] == "queued"
    restarted = SessionMailbox(HUB, quiet_box.directory)
    assert request(restarted, author, target, second)["status"] == "coalesced"
    assert len(launches) == 1
    async with Client(build_session_server(restarted)) as mcp:
        result = (
            await mcp.call_tool(
                "check_inbox", {"context_id": target["context_id"], "full": True}
            )
        ).data
    assert {item["id"] for item in result["items"]} == {first["id"], second["id"]}
    assert request(restarted, author, target, second)["status"] == "queued"
    assert len(launches) == 2
    assert request(restarted, author, target, first)["duplicate"] is True
    assert len(launches) == 2


@pytest.mark.parametrize("failure", ["nonzero", "oserror", "timeout"])
def test_only_ambiguous_failure_keeps_pending(
    quiet_box: SessionMailbox, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    author, target = participants(quiet_box)
    first, second = (deliver(quiet_box, author, target) for _ in range(2))
    attempts: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        attempts.append(args)
        if len(attempts) > 1:
            return subprocess.CompletedProcess(args, 0, "", "")
        if failure == "oserror":
            raise OSError("missing CLI")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", run)
    result = request(quiet_box, author, target, first)
    assert result["status"] == ("unknown" if failure == "timeout" else "failed")
    next_result = request(quiet_box, author, target, second)
    assert next_result["status"] == ("coalesced" if failure == "timeout" else "queued")
    assert len(attempts) == (1 if failure == "timeout" else 2)


async def test_successful_mcp_inbox_fetch_acknowledges_all_pending_mail(
    quiet_box: SessionMailbox, launches: list[list[str]]
) -> None:
    author, target = participants(quiet_box)
    first, second = (deliver(quiet_box, author, target) for _ in range(2))
    assert request(quiet_box, author, target, first)["status"] == "queued"
    assert request(quiet_box, author, target, second)["status"] == "coalesced"
    async with Client(build_session_server(quiet_box)) as mcp:
        result = (
            await mcp.call_tool(
                "check_inbox", {"context_id": target["context_id"], "full": True}
            )
        ).data
    assert {item["id"] for item in result["items"]} == {first["id"], second["id"]}
    third = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, third)["status"] == "queued"
    assert len(launches) == 2


async def test_failed_mcp_inbox_fetch_does_not_acknowledge_pending(
    quiet_box: SessionMailbox,
    launches: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author, target = participants(quiet_box)
    first = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, first)["status"] == "queued"

    def fail(client: HubClient, view: str = "summary", since: str | None = None) -> Any:
        raise ClientError("Hub temporarily unavailable")

    monkeypatch.setattr(HubClient, "check_inbox", fail)
    async with Client(build_session_server(quiet_box)) as mcp:
        with pytest.raises(ToolError, match="Hub temporarily unavailable"):
            await mcp.call_tool("check_inbox", {"context_id": target["context_id"]})
    second = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, second)["status"] == "coalesced"
    assert len(launches) == 1


async def test_other_mailbox_fetch_cannot_acknowledge_recipient_pending(
    quiet_box: SessionMailbox, launches: list[list[str]]
) -> None:
    author, target = participants(quiet_box)
    first = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, first)["status"] == "queued"
    async with Client(build_session_server(quiet_box)) as mcp:
        result = (
            await mcp.call_tool(
                "check_inbox", {"context_id": author["context_id"], "full": True}
            )
        ).data
    assert result["items"] == []
    second = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, second)["status"] == "coalesced"
    assert len(launches) == 1


async def test_failed_pending_write_preserves_successful_mail_fetch(
    quiet_box: SessionMailbox,
    launches: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author, target = participants(quiet_box)
    first = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, first)["status"] == "queued"

    def fail(path: Path, value: dict[str, Any]) -> None:
        raise OSError("Cannot persist acknowledgment")

    monkeypatch.setattr("agent_inbox.session_wake._save", fail)
    async with Client(build_session_server(quiet_box)) as mcp:
        result = (
            await mcp.call_tool(
                "check_inbox", {"context_id": target["context_id"], "full": True}
            )
        ).data
    assert [item["id"] for item in result["items"]] == [first["id"]]
    second = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, second)["status"] == "coalesced"
    assert len(launches) == 1


async def test_busy_wake_lock_does_not_block_mail_fetch_or_acknowledge_pending(
    quiet_box: SessionMailbox,
    launches: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author, target = participants(quiet_box)
    first = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, first)["status"] == "queued"

    @contextmanager
    def busy(path: Path, **kwargs: Any) -> Iterator[None]:
        raise LockUnavailable("Wake delivery still running")
        yield

    with monkeypatch.context() as patch:
        patch.setattr("agent_inbox.session_wake.exclusive", busy)
        async with Client(build_session_server(quiet_box)) as mcp:
            result = (
                await mcp.call_tool(
                    "check_inbox", {"context_id": target["context_id"], "full": True}
                )
            ).data
    assert [item["id"] for item in result["items"]] == [first["id"]]
    second = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, second)["status"] == "coalesced"
    assert len(launches) == 1


@pytest.mark.parametrize(
    "presence,expected",
    [
        ({"active": True, "at": 999.0}, "hook_active"),
        ({"active": True, "at": 955.1}, "hook_active"),
        ({"active": True, "at": 955.0}, "queued"),
        ({"active": True, "at": 900.0}, "queued"),
        ({"active": False, "at": 999.0}, "queued"),
        ({"active": "true", "at": 999.0}, "queued"),
        ({"active": True, "at": True}, "queued"),
        ({"active": True, "at": "now"}, "queued"),
        ({"active": True, "at": float("nan")}, "queued"),
        ({"active": True, "at": float("inf")}, "queued"),
        ({"active": True, "at": 1001.0}, "queued"),
        ({}, "queued"),
        ([], "queued"),
    ],
)
def test_only_recent_valid_hook_presence_suppresses_queue(
    quiet_box: SessionMailbox,
    launches: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    presence: Any,
    expected: str,
) -> None:
    author, target = participants(quiet_box)
    directory = quiet_box.directory.parent / "hook-presence"
    directory.mkdir()
    (directory / f"{target['context_id']}.json").write_text(json.dumps(presence))
    monkeypatch.setattr("agent_inbox.session_wake.time.time", lambda: 1000.0)
    message = deliver(quiet_box, author, target)
    assert request(quiet_box, author, target, message)["status"] == expected
    assert len(launches) == (0 if expected == "hook_active" else 1)
    if expected == "hook_active":
        monkeypatch.setattr("agent_inbox.session_wake.time.time", lambda: 1045.0)
        assert request(quiet_box, author, target, message)["status"] == "queued"
        assert len(launches) == 1
