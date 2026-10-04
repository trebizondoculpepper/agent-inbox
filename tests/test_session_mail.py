"""Несколько контекстов используют настоящий API через один MCP без общей личности."""

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from litestar.testing import TestClient

from agent_inbox.api import build_api
from agent_inbox.cli import main
from agent_inbox.client import ClientError, HubClient
from agent_inbox.house import House
from agent_inbox.mailbox import Mailbox
from agent_inbox.session_client import SessionMailbox
from agent_inbox.session_tools import build_session_server
from agent_inbox.store import InMemoryStore

HUB = "http://hub.invalid"


@pytest.fixture
def api_bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
        yield http


@pytest.fixture
def box(api_bridge: TestClient, tmp_path: Path) -> SessionMailbox:
    return SessionMailbox(HUB, tmp_path / "contexts")


def register(box: SessionMailbox, key: str, **kwargs: Any) -> dict[str, Any]:
    return box.register(key, "test-project", "Проверка связи", "codex", **kwargs)


def test_resume_and_children_preserve_separate_addresses(box: SessionMailbox) -> None:
    root = register(box, "codex-root")
    child = register(box, "codex-child", parent_context=root["context_id"])
    other = register(box, "codex-other")
    assert len({root["address"], child["address"], other["address"]}) == 3
    restarted = SessionMailbox(HUB, box.directory)
    assert register(restarted, "codex-root") == root
    assert (
        register(restarted, "codex-child", parent_context=root["context_id"]) == child
    )
    profile = box.client(child["context_id"]).whois(child["address"])["profile"]
    assert profile["parent"] == root["address"]
    assert profile["project"] == "test-project"
    with pytest.raises(ClientError, match="другому контексту"):
        register(box, "codex-child")
    with pytest.raises(ClientError, match="том же проекте"):
        box.register(
            "bad-child",
            "another-project",
            "Проверка",
            "claude",
            parent_context=root["context_id"],
        )


async def test_one_mcp_keeps_parallel_senders_and_read_receipts_separate(
    box: SessionMailbox,
) -> None:
    async with Client(build_session_server(box)) as mcp:

        async def tool(name: str, **args: Any) -> dict[str, Any]:
            result = await mcp.call_tool(name, args)
            assert isinstance(result.data, dict)
            return result.data

        async def join(key: str) -> dict[str, Any]:
            return await tool(
                "register_session",
                session_key=key,
                project="test-project",
                purpose="Проверка",
                engine="codex",
            )

        first, second, sender = await asyncio.gather(
            join("first"), join("second"), join("sender")
        )
        first_id, second_id = first["context_id"], second["context_id"]
        assert first_id != second_id
        messages = await asyncio.gather(
            tool(
                "send_message",
                context_id=first_id,
                to=[sender["address"]],
                body="Первый",
            ),
            tool(
                "send_message",
                context_id=second_id,
                to=[sender["address"]],
                body="Второй",
            ),
        )
        assert {note["attributedTo"] for note in messages} == {
            f"{HUB}/actors/{first['address']}",
            f"{HUB}/actors/{second['address']}",
        }
        broadcast = await tool(
            "send_message",
            context_id=sender["context_id"],
            to=[first["address"], second["address"]],
            body="Общее письмо",
        )
        for context_id in (first_id, second_id):
            inbox = await tool("check_inbox", context_id=context_id, full=True)
            assert [note["id"] for note in inbox["items"]] == [broadcast["id"]]
        await tool("read_message", context_id=first_id, message_id=broadcast["id"])
        assert (await tool("check_inbox", context_id=first_id, full=True))[
            "totalItems"
        ] == 0
        remaining = await tool("check_inbox", context_id=second_id, full=True)
        assert [note["id"] for note in remaining["items"]] == [broadcast["id"]]
        reply = await tool(
            "reply_message",
            context_id=second_id,
            message_id=broadcast["id"],
            body="Ответ",
        )
        assert reply["attributedTo"] == f"{HUB}/actors/{second['address']}"
        assert reply["inReplyTo"] == broadcast["id"]
        with pytest.raises(ToolError):
            await mcp.call_tool("check_inbox", {})


def test_lost_join_response_resumes_the_saved_pending_actor(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch, api_bridge: TestClient
) -> None:
    original = HubClient.join
    attempts: list[str] = []

    def lost(client: HubClient, name: str | None = None) -> Any:
        original(client, name)
        attempts.append(client.config.name)
        raise ClientError("Ответ потерян после успешного join")

    monkeypatch.setattr(HubClient, "join", lost)
    with pytest.raises(ClientError, match="Ответ потерян"):
        register(box, "retry")
    pending_files = list(box.directory.glob("*.json"))
    assert len(pending_files) == 1
    pending = json.loads(pending_files[0].read_text())
    assert not pending["ready"]
    assert attempts == [pending["address"]]
    assert api_bridge.get(f"/actors/{pending['address']}").status_code == 200
    resumed = register(SessionMailbox(HUB, box.directory), "retry")
    assert resumed["ready"]
    assert resumed["address"] == pending["address"]
    assert len(attempts) == 1
    actors = api_bridge.get("/actors").json()["items"]
    assert [
        actor["preferredUsername"]
        for actor in actors
        if actor["preferredUsername"].startswith("a_")
    ] == [pending["address"]]


def test_concurrent_registration_of_one_key_is_idempotent(box: SessionMailbox) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: register(box, "same-key"), range(4)))
    assert all(record["ready"] for record in records)
    assert len({record["address"] for record in records}) == 1
    assert len(list(box.directory.glob("*.json"))) == 1


def test_unknown_or_wrong_hub_context_never_falls_back(box: SessionMailbox) -> None:
    known = register(box, "known")
    with pytest.raises(ClientError, match="отсутствует"):
        box.client("0" * 64)
    with pytest.raises(ClientError, match="Некорректный context_id"):
        box.client("../known")
    with pytest.raises(ClientError, match="другому серверу"):
        SessionMailbox("http://other.invalid", box.directory).client(
            known["context_id"]
        )


def test_bound_token_is_rejected_before_join_and_before_existing_context_use(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch, api_bridge: TestClient
) -> None:
    known = register(box, "known")
    before = api_bridge.get("/actors").json()["totalItems"]
    original = HubClient.remote_doctor

    def bound(client: HubClient) -> Any:
        report = original(client)
        report["hub"]["authMode"] = "enforce"
        report["you"]["verified"] = "another_actor"
        report["you"]["token"] = "accepted"
        return report

    monkeypatch.setattr(HubClient, "remote_doctor", bound)
    with pytest.raises(ClientError, match="другому участнику"):
        register(box, "new")
    with pytest.raises(ClientError, match="другому участнику"):
        box.client(known["context_id"])
    assert api_bridge.get("/actors").json()["totalItems"] == before


def test_shared_token_is_accepted(
    box: SessionMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HubClient.remote_doctor

    def shared(client: HubClient) -> Any:
        report = original(client)
        report["hub"]["authMode"] = "enforce"
        report["you"]["verified"] = "*"
        report["you"]["token"] = "accepted"
        return report

    monkeypatch.setattr(HubClient, "remote_doctor", shared)
    record = register(box, "shared-token")
    assert box.client(record["context_id"]).config.name == record["address"]


def test_status_preserves_profile_and_resume_preserves_status(
    box: SessionMailbox,
) -> None:
    record = register(box, "task", worktree="/tmp/test-worktree")
    client = box.client(record["context_id"])
    profile = client.whois(record["address"])["profile"]
    profile["extra"] = {"test": True}
    client.update_profile(profile)
    updated = box.status(record["context_id"], "completed")
    assert updated["profile"] == {**profile, "status": "completed", "groups": []}
    register(SessionMailbox(HUB, box.directory), "task")
    assert client.whois(record["address"])["profile"] == updated["profile"]


def test_cli_uses_same_context_without_touching_legacy_config(
    box: SessionMailbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    config = tmp_path / "agent-inbox.toml"
    original = 'hub = "http://legacy.invalid"\nname = "legacy_actor"\n'
    config.write_text(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AGENT_INBOX_HUB", HUB)
    monkeypatch.setenv("AGENT_INBOX_SESSIONS_DIR", str(box.directory))
    monkeypatch.delenv("AGENT_INBOX_TOKEN", raising=False)
    args = {
        "session_key": "cli",
        "project": "test-project",
        "purpose": "Проверка",
        "engine": "claude",
    }
    assert main(["session", "call", "register_session", json.dumps(args)]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["ready"]
    assert (
        main(
            [
                "session",
                "call",
                "whoami",
                json.dumps({"context_id": record["context_id"]}),
            ]
        )
        == 0
    )
    actor = json.loads(capsys.readouterr().out)
    assert actor["preferredUsername"] == record["address"]
    assert config.read_text() == original
    assert not (tmp_path / "xdg").exists()
