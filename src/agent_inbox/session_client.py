"""Persistent task contexts for several participants of one client."""

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


def project_group(project: str) -> str:
    # Addresses are lowercased, but project identifiers are case-sensitive.
    encoded = "".join(
        chr(byte)
        if 97 <= byte <= 122 or 48 <= byte <= 57 or byte in b"-_."
        else f"%{byte:02x}"
        for byte in project.encode("utf-8")
    )
    return "project:" + encoded


def _membership(profile: dict[str, Any], project: str) -> None:
    group = project_group(project)
    groups = set(profile.get("groups") or [])
    if profile.get("status") == "completed":
        groups.discard(group)
    else:
        groups.add(group)
    profile["groups"] = sorted(groups)


def _display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ClientError("The name must be 1 to 80 characters, with no line breaks.")
    return name


class SessionMailbox:
    def __init__(self, hub: str, directory: Path, token: str | None = None) -> None:
        self.hub = hub.rstrip("/")
        self.directory = directory
        self.token = token
        self.client_type = HubClient
        self.timeout = 10.0

    @classmethod
    def from_env(cls) -> SessionMailbox:
        shared = load_global()
        hub = os.environ.get("AGENT_INBOX_HUB") or shared.get("hub")
        if not hub:
            raise ClientError("Set AGENT_INBOX_HUB to the shared mail server.")
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
            raise ClientError("Invalid context_id. Use register_session.")
        return self.directory / f"{context_id}.json"

    def _read(self, context_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._path(context_id).read_text())
        except (OSError, ValueError) as exc:
            raise ClientError("The context is missing or corrupt.") from exc
        if not isinstance(value, dict) or value.get("context_id") != context_id:
            raise ClientError("Invalid context file.")
        if value.get("hub") != self.hub:
            raise ClientError("The context belongs to a different mail server.")
        if not isinstance(value.get("address"), str) or not re.fullmatch(
            r"a_[a-f0-9]{32}", value["address"]
        ):
            raise ClientError("Invalid address in the context.")
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
        client = self.client_type(
            Config(hub=self.hub, name=record["address"], token=self.token),
            timeout=self.timeout,
        )
        report = client.remote_doctor()
        you = report.get("you", {})
        mode = report.get("hub", {}).get("authMode")
        verified = you.get("verified")
        if mode not in {"off", "warn", "enforce"}:
            raise ClientError("The server did not confirm its identity-check mode.")
        if verified not in {None, "*", record["address"]}:
            raise ClientError(
                "The token belongs to a different participant. "
                "A shared token is required."
            )
        if mode == "enforce" and verified not in {"*", record["address"]}:
            raise ClientError("The server did not confirm this context's credentials.")
        known = you.get("known")
        if not isinstance(known, bool):
            raise ClientError("The server did not confirm that the address exists.")
        return client, known

    def register(
        self,
        session_key: str,
        project: str,
        purpose: str,
        engine: str,
        worktree: str = "",
        parent_context: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        if not all(x.strip() for x in (session_key, project, purpose, engine)):
            raise ClientError("Session key, project, task and client are required.")
        if display_name is not None:
            display_name = _display_name(display_name)
        identity = json.dumps([self.hub, session_key], ensure_ascii=False)
        context_id = hashlib.sha256(identity.encode()).hexdigest()
        parent = self._read(parent_context) if parent_context else None
        if parent and (not parent.get("ready") or parent.get("project") != project):
            raise ClientError("The parent must be registered in the same project.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(context_id)
        # The lock covers the HTTP calls and is released after a failure.
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
                    raise ClientError(
                        "The key already belongs to another task context."
                    )
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
                # The name saved before the HTTP call lets a lost response be recovered.
                self._write(record)
            client, known = self._checked_client(record)
            if record["ready"]:
                if not known:
                    raise ClientError(
                        "The address has disappeared from the "
                        "server. Check its database."
                    )
                actor = client.whois(record["address"])
                profile = dict(actor.get("profile") or {})
                before = dict(profile)
                profile.setdefault("display_name", record["purpose"][:80])
                if display_name is not None:
                    profile["display_name"] = display_name
                _membership(profile, project)
                if profile != before:
                    client.update_profile(profile)
                return record | {
                    "display_name": profile["display_name"],
                    "project_group": project_group(project),
                }
            if not known:
                client.join(record["address"])
            profile = {
                "project": project,
                "engine": engine,
                "purpose": record["purpose"],
                "worktree": record["worktree"],
                "parent": record["parent_address"],
                "status": "active",
                "display_name": display_name or record["purpose"][:80],
            }
            _membership(profile, project)
            client.update_profile(profile)
            record["ready"] = True
            self._write(record)
            return record | {
                "display_name": profile["display_name"],
                "project_group": project_group(project),
            }

    def client(self, context_id: str) -> HubClient:
        record = self._read(context_id)
        if not record.get("ready"):
            raise ClientError("Registration is incomplete. Repeat register_session.")
        client, known = self._checked_client(record)
        if not known:
            raise ClientError("The context was not found on the server.")
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
            _membership(profile, self._read(context_id)["project"])
            return client.update_profile(profile)

    def update_profile(
        self,
        context_id: str,
        display_name: str,
        purpose: str | None = None,
        worktree: str | None = None,
    ) -> Any:
        name = _display_name(display_name)
        if purpose is not None and not purpose.strip():
            raise ClientError("The task description cannot be empty.")
        with exclusive(
            self._path(context_id).with_suffix(".lock"), timeout=5, stale_after=3600
        ):
            client = self.client(context_id)
            actor = client.whois(client.config.name)
            profile = dict(actor.get("profile") or {})
            profile["display_name"] = name
            if purpose is not None:
                profile["purpose"] = purpose
            if worktree is not None:
                profile["worktree"] = worktree
            _membership(profile, self._read(context_id)["project"])
            return client.update_profile(profile)
