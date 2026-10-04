"""Сохраняемые контексты задач для нескольких участников одного клиента."""

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from agent_inbox.client import ClientError, Config, HubClient, load_global
from agent_inbox.locking import exclusive


class SessionMailbox:
    def __init__(self, hub: str, directory: Path, token: str | None = None) -> None:
        self.hub = hub.rstrip("/")
        self.directory = directory
        self.token = token

    @classmethod
    def from_env(cls) -> SessionMailbox:
        shared = load_global()
        hub = os.environ.get("AGENT_INBOX_HUB") or shared.get("hub")
        if not hub:
            raise ClientError("Задайте AGENT_INBOX_HUB для общего сервера почты.")
        directory = Path(
            os.environ.get(
                "AGENT_INBOX_SESSIONS_DIR",
                str(Path.home() / ".local/share/agent-inbox/sessions"),
            )
        ).expanduser()
        token = os.environ.get("AGENT_INBOX_TOKEN") or shared.get("token")
        return cls(str(hub), directory, str(token) if token else None)

    def _path(self, context_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", context_id):
            raise ClientError("Некорректный context_id. Используйте register_session.")
        return self.directory / f"{context_id}.json"

    def _read(self, context_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._path(context_id).read_text())
        except (OSError, ValueError) as exc:
            raise ClientError("Контекст отсутствует или повреждён.") from exc
        if not isinstance(value, dict) or value.get("context_id") != context_id:
            raise ClientError("Некорректный файл контекста.")
        if value.get("hub") != self.hub:
            raise ClientError("Контекст принадлежит другому серверу почты.")
        if not isinstance(value.get("address"), str) or not re.fullmatch(
            r"a_[a-f0-9]{32}", value["address"]
        ):
            raise ClientError("Некорректный адрес в контексте.")
        return value

    def _write(self, record: dict[str, Any]) -> None:
        target = self._path(record["context_id"])
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".session-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def _checked_client(self, record: dict[str, Any]) -> tuple[HubClient, bool]:
        client = HubClient(
            Config(hub=self.hub, name=record["address"], token=self.token), timeout=10
        )
        report = client.remote_doctor()
        you = report.get("you", {})
        mode = report.get("hub", {}).get("authMode")
        verified = you.get("verified")
        if mode not in {"off", "warn", "enforce"}:
            raise ClientError("Сервер не подтвердил режим проверки личности.")
        if verified not in {None, "*", record["address"]}:
            raise ClientError("Токен принадлежит другому участнику. Нужен общий токен.")
        if mode == "enforce" and verified not in {"*", record["address"]}:
            raise ClientError("Сервер не подтвердил credentials этого контекста.")
        known = you.get("known")
        if not isinstance(known, bool):
            raise ClientError("Сервер не подтвердил наличие адреса.")
        return client, known

    def register(
        self,
        session_key: str,
        project: str,
        purpose: str,
        engine: str,
        worktree: str = "",
        parent_context: str | None = None,
    ) -> dict[str, Any]:
        if not all(x.strip() for x in (session_key, project, purpose, engine)):
            raise ClientError("Ключ сессии, проект, задача и клиент обязательны.")
        identity = json.dumps([self.hub, session_key], ensure_ascii=False)
        context_id = hashlib.sha256(identity.encode()).hexdigest()
        parent = self._read(parent_context) if parent_context else None
        if parent and (not parent.get("ready") or parent.get("project") != project):
            raise ClientError("Родитель должен быть зарегистрирован в том же проекте.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(context_id)
        # Блокировка покрывает HTTP-вызовы и освобождается после сбоя.
        with exclusive(path.with_suffix(".lock"), timeout=5, stale_after=3600):
            if path.exists():
                record = self._read(context_id)
                if any(
                    record.get(key) != value
                    for key, value in {
                        "project": project,
                        "engine": engine,
                        "parent_context": parent_context,
                    }.items()
                ):
                    raise ClientError("Ключ уже принадлежит другому контексту задачи.")
            else:
                record = {
                    "context_id": context_id,
                    "address": f"a_{uuid.uuid4().hex}",
                    "hub": self.hub,
                    "project": project,
                    "engine": engine,
                    "parent_context": parent_context,
                    "parent_address": parent["address"] if parent else None,
                    "purpose": purpose,
                    "worktree": worktree,
                    "ready": False,
                }
                # Сохранённое до HTTP имя позволяет восстановить потерянный ответ.
                self._write(record)
            client, known = self._checked_client(record)
            if record["ready"]:
                if not known:
                    raise ClientError(
                        "Адрес исчез на сервере. Проверьте его базу данных."
                    )
                return record
            if not known:
                client.join(record["address"])
            profile = {
                "project": project,
                "engine": engine,
                "purpose": record["purpose"],
                "worktree": record["worktree"],
                "parent": record["parent_address"],
                "status": "active",
            }
            client.update_profile(profile)
            record["ready"] = True
            self._write(record)
            return record

    def client(self, context_id: str) -> HubClient:
        record = self._read(context_id)
        if not record.get("ready"):
            raise ClientError("Регистрация не завершена. Повторите register_session.")
        client, known = self._checked_client(record)
        if not known:
            raise ClientError("Контекст не найден на сервере.")
        return client

    def status(
        self, context_id: str, status: Literal["active", "waiting", "completed"]
    ) -> Any:
        path = self._path(context_id)
        with exclusive(path.with_suffix(".lock"), timeout=5, stale_after=3600):
            client = self.client(context_id)
            actor = client.whois(client.config.name)
            profile = dict(actor.get("profile") or {})
            profile["status"] = status
            return client.update_profile(profile)
