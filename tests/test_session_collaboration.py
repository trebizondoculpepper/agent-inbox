"""Имена, проектные группы и ответы проверяются через настоящий API и MCP."""

import html
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from litestar.testing import TestClient

from agent_inbox.api import build_api
from agent_inbox.client import ClientError, Config, HubClient
from agent_inbox.console import build_console
from agent_inbox.house import House
from agent_inbox.mailbox import Mailbox
from agent_inbox.session_client import SessionMailbox
from agent_inbox.session_tools import build_session_server
from agent_inbox.store import InMemoryStore

HUB = "http://hub.invalid"


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionMailbox]:
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


def join(
    box: SessionMailbox, key: str, project: str = "project-one", name: str | None = None
) -> dict[str, Any]:
    return box.register(key, project, "Проверка " + key, "codex", display_name=name)


def inbox(box: SessionMailbox, record: dict[str, Any]) -> list[dict[str, Any]]:
    return box.client(record["context_id"]).check_inbox(view="full")["items"]


async def test_project_group_has_exact_members_and_status_controls_future_delivery(
    box: SessionMailbox,
) -> None:
    sender, active, finished = (
        join(box, key) for key in ("sender", "active", "finished")
    )
    outsider = join(box, "outsider", "project-two")
    box.status(finished["context_id"], "completed")
    async with Client(build_session_server(box)) as mcp:
        first = (
            await mcp.call_tool(
                "send_project_message",
                {
                    "context_id": sender["context_id"],
                    "subject": "Обновление",
                    "body": "Первая рассылка",
                },
            )
        ).data
        assert isinstance(first, dict)
        assert first["to"] == [f"{HUB}/actors/{active['address']}"]
        assert [note["id"] for note in inbox(box, active)] == [first["id"]]
        assert inbox(box, outsider) == []
        assert inbox(box, finished) == []
        assert inbox(box, sender) == []
        box.status(finished["context_id"], "active")
        second = (
            await mcp.call_tool(
                "send_project_message",
                {
                    "context_id": sender["context_id"],
                    "subject": "Ещё обновление",
                    "body": "Вторая рассылка",
                },
            )
        ).data
        assert isinstance(second, dict)
        assert set(second["to"]) == {
            f"{HUB}/actors/{active['address']}",
            f"{HUB}/actors/{finished['address']}",
        }
        assert [note["id"] for note in inbox(box, finished)] == [second["id"]]
        assert inbox(box, outsider) == []


async def test_rename_keeps_address_old_messages_and_searchable_profile(
    box: SessionMailbox,
) -> None:
    sender = join(box, "sender", name="Старое имя")
    reader = join(box, "reader", name="Читатель")
    other = join(box, "other", "another-project", "Другое имя")
    finished = join(box, "finished", name="Завершённый")
    box.status(finished["context_id"], "completed")
    original = box.client(sender["context_id"]).send_message(
        reader["address"], "История"
    )
    async with Client(build_session_server(box)) as mcp:
        changed = (
            await mcp.call_tool(
                "update_profile",
                {
                    "context_id": sender["context_id"],
                    "display_name": "Новый Проверяющий",
                    "purpose": "Проверка графики",
                },
            )
        ).data
        assert isinstance(changed, dict)
        assert changed["preferredUsername"] == sender["address"]
        assert changed["name"] == "Новый Проверяющий"
        result = (
            await mcp.call_tool(
                "list_agents", {"context_id": reader["context_id"], "query": "НОВЫЙ"}
            )
        ).data
        assert result["totalItems"] == 1
        assert result["items"][0]["preferredUsername"] == sender["address"]
        listing = (
            await mcp.call_tool("list_agents", {"context_id": reader["context_id"]})
        ).data
        assert {a["preferredUsername"] for a in listing["items"]} == {
            sender["address"],
            reader["address"],
        }
        complete = (
            await mcp.call_tool(
                "list_agents",
                {"context_id": reader["context_id"], "include_completed": True},
            )
        ).data
        assert finished["address"] in {
            a["preferredUsername"] for a in complete["items"]
        }
        every = (
            await mcp.call_tool(
                "list_agents",
                {"context_id": reader["context_id"], "all_projects": True},
            )
        ).data
        assert other["address"] in {a["preferredUsername"] for a in every["items"]}
    stored = box.client(reader["context_id"]).peek_message(original["id"])
    assert stored["content"] == "История"
    assert stored["attributedTo"] == original["attributedTo"]
    assert join(box, "sender")["display_name"] == "Новый Проверяющий"


async def test_reply_all_uses_original_sender_to_cc_but_not_new_group_members(
    box: SessionMailbox,
) -> None:
    sender, first, cc = (join(box, key) for key in ("sender", "first", "cc"))
    client = box.client(sender["context_id"])
    original = client._call(
        "POST",
        f"/actors/{sender['address']}/outbox",
        {
            "type": "Note",
            "to": [sender["project_group"]],
            "cc": [cc["address"]],
            "content": "Всем участникам",
        },
    )
    newcomer = join(box, "newcomer")
    assert inbox(box, newcomer) == []
    async with Client(build_session_server(box)) as mcp:
        reply = (
            await mcp.call_tool(
                "reply_all",
                {
                    "context_id": first["context_id"],
                    "message_id": original["id"],
                    "body": "Ответ всем исходным участникам",
                },
            )
        ).data
    assert isinstance(reply, dict)
    assert set(reply["to"]) == {
        f"{HUB}/actors/{sender['address']}",
        f"{HUB}/actors/{cc['address']}",
    }
    assert reply["inReplyTo"] == original["id"]
    assert [note["id"] for note in inbox(box, sender)] == [reply["id"]]
    assert reply["id"] in {note["id"] for note in inbox(box, cc)}
    assert inbox(box, first) == []
    assert inbox(box, newcomer) == []


def test_reply_all_keeps_cc_and_private_branches_private(box: SessionMailbox) -> None:
    sender, first, cc = (join(box, key) for key in ("sender", "first", "cc"))
    client = box.client(sender["context_id"])
    root = client._call(
        "POST",
        f"/actors/{sender['address']}/outbox",
        {
            "type": "Note",
            "to": [first["address"]],
            "cc": [cc["address"]],
            "content": "Общее начало",
        },
    )
    assert root["cc"] == [f"{HUB}/actors/{cc['address']}"]
    first_client = box.client(first["context_id"])
    everyone = first_client.reply_message(root["id"], "Всем", reply_all=True)
    assert set(everyone["to"]) == {
        f"{HUB}/actors/{sender['address']}",
        f"{HUB}/actors/{cc['address']}",
    }
    private = first_client.reply_message(root["id"], "Приватная ветка")
    assert private["to"] == [f"{HUB}/actors/{sender['address']}"]
    answered = client.reply_message(
        private["id"], "Ответ в приватной ветке", reply_all=True
    )
    assert answered["to"] == [f"{HUB}/actors/{first['address']}"]
    cc_client = box.client(cc["context_id"])
    visible = cc_client.read_thread(root["id"])
    ids = {note["id"] for note in visible["items"]}
    assert {root["id"], everyone["id"]} <= ids
    assert not {private["id"], answered["id"]} & ids
    with pytest.raises(ClientError):
        cc_client.reply_message(private["id"], "Не должен увидеть", reply_all=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"replyAll": "yes"},
        {"replyAll": True},
        {"replyAll": True, "inReplyTo": "unknown", "to": ["admin"]},
    ],
)
def test_reply_all_rejects_ambiguous_payload(
    box: SessionMailbox, payload: dict[str, Any]
) -> None:
    sender = join(box, "sender")
    with pytest.raises(ClientError):
        box.client(sender["context_id"])._call(
            "POST",
            f"/actors/{sender['address']}/outbox",
            {"type": "Note", "content": "Неверный ответ", **payload},
        )


def test_console_escapes_display_name_and_keeps_opaque_address_routes(
    box: SessionMailbox,
) -> None:
    dangerous = '<img src=x onerror="alert(1)">'
    record = join(box, "dangerous", name=dangerous)
    observer = HubClient(Config(hub=HUB, name="console"))
    with TestClient(app=build_console(observer)) as console:
        labels = console.get("/agent-labels")
        assert labels.status_code == 200
        assert dangerous in labels.json()[record["address"]]
        page = console.get(f"/agent/{record['address']}")
        assert page.status_code == 200
        assert html.escape(dangerous) in page.text
        assert dangerous not in page.text
        assert f"/mailbox/{record['address']}" in page.text
        realtime = console.get("/realtime")
        assert realtime.status_code == 200
        assert dangerous not in realtime.text
        assert "data-labels=" in realtime.text


def test_project_names_with_distinct_identity_do_not_share_delivery(
    box: SessionMailbox,
) -> None:
    sender = join(box, "sender", "Project")
    teammate = join(box, "teammate", "Project")
    other = join(box, "other", "project")
    sent = box.client(sender["context_id"]).send_message(
        sender["project_group"], "Только наш проект"
    )
    assert [note["id"] for note in inbox(box, teammate)] == [sent["id"]]
    assert inbox(box, other) == []


def test_old_client_completed_status_also_leaves_project_delivery(
    box: SessionMailbox,
) -> None:
    sender, finished, witness = (join(box, key) for key in ("sender", "old", "witness"))
    client = box.client(finished["context_id"])
    profile = client.whois(finished["address"])["profile"]
    assert profile["groups"] == [sender["project_group"]]
    profile["status"] = "completed"
    client.update_profile(profile)
    message = box.client(sender["context_id"]).send_message(
        sender["project_group"], "Проверка старого клиента"
    )
    assert [note["id"] for note in inbox(box, witness)] == [message["id"]]
    assert inbox(box, finished) == []
