"""Самостоятельная привязка разрешена корневому Codex и сохраняет почту при отказе."""

import json
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

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
PROJECT = "test-project"


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


def policy(box: SessionMailbox, **changes: Any) -> dict[str, Any]:
    config = {
        "hub": HUB,
        "codex_command": "/opt/example/codex",
        "self_registration_projects": [PROJECT],
        "targets": {},
        "operator_note": "Сохранить остальные настройки",
        **changes,
    }
    SessionWake(box).config_path.write_text(json.dumps(config))
    return config


def join(box: SessionMailbox, key: str, **kwargs: Any) -> dict[str, Any]:
    return box.register(
        key, PROJECT, "Проверка привязки", kwargs.pop("engine", "codex"), **kwargs
    )


@pytest.mark.parametrize("engine", ["codex", "codex-cli", "codex-desktop"])
def test_root_self_registration_is_idempotent_and_never_wakes(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    record = join(box, "target", engine=engine)
    config = policy(box)

    def forbidden(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        pytest.fail("Регистрация не должна запускать процессы")

    monkeypatch.setattr("agent_inbox.session_wake.subprocess.run", forbidden)
    wake = SessionWake(box)
    result = wake.configure(record["context_id"], THREAD.upper())
    assert result == {
        "supported": True,
        "backend": "codex_queue",
        "status": "configured",
    }
    saved = json.loads(wake.config_path.read_text())
    assert saved["targets"] == {
        record["address"]: {
            "enabled": True,
            "backend": "codex_queue",
            "project": PROJECT,
            "context_id": record["context_id"],
            "thread_id": THREAD,
        }
    }
    assert saved["operator_note"] == config["operator_note"]
    before = wake.config_path.read_bytes()
    assert wake.configure(record["context_id"], THREAD) == result
    assert wake.config_path.read_bytes() == before
    assert wake.capability(record["address"], PROJECT)["supported"] is True


@pytest.mark.parametrize("engine", ["claude", "claude-code", "other", "Codex"])
def test_non_codex_cannot_claim_a_codex_chat(box: SessionMailbox, engine: str) -> None:
    record = join(box, "target", engine=engine)
    policy(box)
    wake = SessionWake(box)
    before = wake.config_path.read_bytes()
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], THREAD)
    assert wake.config_path.read_bytes() == before


def test_subagent_cannot_bind_its_parent_chat(box: SessionMailbox) -> None:
    parent = join(box, "parent")
    child = join(box, "child", parent_context=parent["context_id"])
    policy(box)
    wake = SessionWake(box)
    with pytest.raises(ClientError):
        wake.configure(child["context_id"], THREAD)
    assert json.loads(wake.config_path.read_text())["targets"] == {}
    assert wake.configure(parent["context_id"], THREAD)["supported"] is True


def test_unfinished_registration_cannot_become_wake_target(box: SessionMailbox) -> None:
    record = join(box, "target")
    record["ready"] = False
    box._write(record)
    policy(box)
    wake = SessionWake(box)
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], THREAD)
    assert json.loads(wake.config_path.read_text())["targets"] == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"hub": "http://other.invalid"},
        {"self_registration_projects": []},
        {"self_registration_projects": PROJECT},
        {"self_registration_projects": [PROJECT, 17]},
        {"self_registration_projects": [PROJECT.upper()]},
        {"targets": []},
        {"codex_command": "codex"},
        {"codex_command": "/opt/co\x00dex"},
        {"codex_command": None},
    ],
)
def test_operator_policy_fails_closed(
    box: SessionMailbox, changes: dict[str, Any]
) -> None:
    record = join(box, "target")
    policy(box, **changes)
    wake = SessionWake(box)
    before = wake.config_path.read_bytes()
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], THREAD)
    assert wake.config_path.read_bytes() == before


@pytest.mark.parametrize("content", [None, "[]", "{bad json", '{"hub": "x"}'])
def test_missing_or_malformed_policy_is_not_created_or_repaired(
    box: SessionMailbox, content: str | None
) -> None:
    record = join(box, "target")
    wake = SessionWake(box)
    if content is not None:
        wake.config_path.write_text(content)
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], THREAD)
    if content is None:
        assert not wake.config_path.exists()
    else:
        assert wake.config_path.read_text() == content


@pytest.mark.parametrize("thread", ["", "--thread=evil", "g" * 32, "not-a-uuid"])
def test_invalid_thread_cannot_change_policy(box: SessionMailbox, thread: str) -> None:
    record = join(box, "target")
    policy(box)
    wake = SessionWake(box)
    before = wake.config_path.read_bytes()
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], thread)
    assert wake.config_path.read_bytes() == before


@pytest.mark.parametrize(
    "conflict", ["disabled", "integer-enabled", "other-thread", "other-context"]
)
def test_existing_binding_cannot_be_overwritten(
    box: SessionMailbox, conflict: str
) -> None:
    record = join(box, "target")
    policy(box)
    wake = SessionWake(box)
    assert wake.configure(record["context_id"], THREAD)["supported"] is True
    config = json.loads(wake.config_path.read_text())
    target = config["targets"][record["address"]]
    if conflict == "disabled":
        target["enabled"] = False
    elif conflict == "integer-enabled":
        target["enabled"] = 1
    elif conflict == "other-thread":
        target["thread_id"] = str(uuid4())
    else:
        target["context_id"] = "a" * 64
    wake.config_path.write_text(json.dumps(config))
    before = wake.config_path.read_bytes()
    with pytest.raises(ClientError):
        wake.configure(record["context_id"], THREAD)
    assert wake.config_path.read_bytes() == before


@pytest.mark.parametrize("enabled", [True, False])
def test_thread_already_bound_to_another_address_is_reserved(
    box: SessionMailbox, enabled: bool
) -> None:
    first, second = join(box, "first"), join(box, "second")
    policy(box)
    wake = SessionWake(box)
    wake.configure(first["context_id"], THREAD)
    config = json.loads(wake.config_path.read_text())
    config["targets"][first["address"]]["enabled"] = enabled
    wake.config_path.write_text(json.dumps(config))
    before = wake.config_path.read_bytes()
    with pytest.raises(ClientError):
        wake.configure(second["context_id"], THREAD.upper())
    assert wake.config_path.read_bytes() == before


def test_concurrent_registrations_keep_every_binding(box: SessionMailbox) -> None:
    records = [join(box, f"target-{i}") for i in range(8)]
    policy(box)
    threads = [str(uuid4()) for _ in records]

    def configure(pair: tuple[dict[str, Any], str]) -> dict[str, Any]:
        record, thread = pair
        return SessionWake(box).configure(record["context_id"], thread)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(configure, zip(records, threads, strict=True)))
    assert len(results) == 8
    assert all(result["supported"] for result in results)
    targets = json.loads(SessionWake(box).config_path.read_text())["targets"]
    assert set(targets) == {record["address"] for record in records}
    assert {target["thread_id"] for target in targets.values()} == set(threads)


async def test_mcp_configures_existing_mailbox_without_changing_address(
    box: SessionMailbox,
) -> None:
    record = join(box, "existing")
    policy(box)
    async with Client(build_session_server(box)) as mcp:
        result = (
            await mcp.call_tool(
                "configure_wake",
                {"context_id": record["context_id"], "codex_thread_id": THREAD},
            )
        ).data
        agents = (
            await mcp.call_tool("list_agents", {"context_id": record["context_id"]})
        ).data
    assert result["status"] == "configured"
    assert len(agents["items"]) == 1
    assert agents["items"][0]["preferredUsername"] == record["address"]
    assert agents["items"][0]["wake"]["supported"] is True


@pytest.mark.parametrize("allowed", [True, False])
async def test_registration_preserves_mail_even_when_optional_wake_fails(
    box: SessionMailbox, allowed: bool
) -> None:
    policy(box, self_registration_projects=[PROJECT] if allowed else [])
    params = {
        "session_key": "new",
        "project": PROJECT,
        "purpose": "Новая сессия",
        "engine": "codex",
        "codex_thread_id": THREAD,
    }
    async with Client(build_session_server(box)) as mcp:
        result = (await mcp.call_tool("register_session", params)).data
        assert result["ready"] is True
        assert result["wake"]["supported"] is allowed
        assert result["wake"]["status"] == ("configured" if allowed else "failed")
        if not allowed:
            assert result["wake"]["detail"]
        again = (await mcp.call_tool("register_session", params)).data
        assert again["address"] == result["address"]
        assert again["context_id"] == result["context_id"]
    sender = join(box, "sender")
    message = box.client(sender["context_id"]).send_message(
        [result["address"]], "Почта работает независимо от wake", "Проверка"
    )
    inbox = box.client(result["context_id"]).check_inbox(view="full")
    assert [item["id"] for item in inbox["items"]] == [message["id"]]


async def test_register_without_thread_does_not_use_shared_mcp_environment(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy(box)
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    async with Client(build_session_server(box)) as mcp:
        result = (
            await mcp.call_tool(
                "register_session",
                {
                    "session_key": "no-thread",
                    "project": PROJECT,
                    "purpose": "Без привязки",
                    "engine": "codex",
                },
            )
        ).data
    assert result["ready"] is True
    assert json.loads(SessionWake(box).config_path.read_text())["targets"] == {}


async def test_mcp_does_not_accept_agent_supplied_command(box: SessionMailbox) -> None:
    record = join(box, "target")
    policy(box)
    async with Client(build_session_server(box)) as mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "configure_wake",
                {
                    "context_id": record["context_id"],
                    "codex_thread_id": THREAD,
                    "codex_command": "/tmp/agent-controlled",
                },
            )
    assert json.loads(SessionWake(box).config_path.read_text())["targets"] == {}
