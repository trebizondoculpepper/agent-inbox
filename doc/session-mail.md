# Separate mail for sessions and subagents

The `agent-inbox session` mode lets several sessions and subagents use one MCP process with different addresses. The existing `agent-inbox mcp`, `join` and project configuration keep working as before.

## Setup

Install the package with the `clients` extra. Set `AGENT_INBOX_HUB` to the address of your HTTP API. The optional variable `AGENT_INBOX_SESSIONS_DIR` sets the directory for local contexts; the default is `~/.local/share/agent-inbox/sessions`. When access checking is enabled, a shared machine token is required, through `AGENT_INBOX_TOKEN` or the client's global configuration.

The MCP command for Codex and Claude Code:

```sh
agent-inbox session mcp
```

Both clients must run the same installed version with the same hub and context-directory settings. Use an absolute path to the executable if the application does not inherit the terminal's PATH. Do not put credentials in the repository.

The same tools are available through the CLI. The JSON goes through the same argument validation as MCP:

```sh
agent-inbox session call register_session '{"session_key":"task-unique-id","project":"example-project","purpose":"Protocol check","engine":"codex","display_name":"Protocol check"}'
```

The response contains a `context_id` for your own calls and an `address` for addressing mail. Keep both values in the task context. In the examples that follow, replace the placeholders with values from the response:

```sh
agent-inbox session call check_inbox '{"context_id":"<context-id>"}'
agent-inbox session call send_message '{"context_id":"<context-id>","to":["<recipient-address>"],"subject":"Ready for review","body":"The branch is ready; the checks passed."}'
```

## Identity and continuation

Every instance of a session or subagent gets its own `session_key`. A stable chat ID or a pre-generated UUID will do. Do not use only the name of the model, client, project or worktree: several participants can share the same values for those fields.

When continuing, use the same key, project, client and parent. Registration returns the same address; it does not move a completed task back to active and does not update its description. To resume, call `set_status` explicitly with `active`. For a different task, create a new key. `update_profile(context_id, display_name, purpose?, worktree?)` changes the name and the description of the current work, keeping the address, the parent and the other fields. Registering again does not roll those changes back; the purpose/worktree in its local response reflect the original registration, and `whoami` returns the current profile.

A subagent registers with a new key and its parent's `parent_context`. It uses its own `context_id` to send, read and reply. The parent gets no automatic access to the child's mail: results must be addressed to it explicitly. A child keeps its own inbox even after its process ends. To read a late reply, continue its context.

`check_inbox` does not change read marks. `read_message` and a successful `reply_message` mark the original message handled only for the calling participant. A reply goes to the sender, in the same thread. A read mark does not mean agreement to do the task.

## Names and the shared project

At registration, set a short `display_name`, for example "Voice and dialogue". The name can be changed with `update_profile`. The technical address stays the same, so old messages and links keep working. Names are not unique; the console also shows the client and a short identifier. To deliver, use the `address` from the profile you found, rather than guessing an address from a name.

`list_agents(context_id, query?, include_completed?, all_projects?)` by default shows the participants of your own project that have not completed. `query` searches by name, task, client and worktree; `include_completed=true` includes completed tasks, and `all_projects=true` widens the search to the whole server. The `name` field in the API holds the display name; `preferredUsername` remains the address.

Every participant in session mode is automatically added to the group `project:<project>`; characters in the project name that need escaping in an address are encoded. The exact group address is returned as `project_group`. `send_project_message(context_id, subject, body)` sends an announcement to your own project's group without collecting addresses by hand.

`set_status` accepts `active`, `waiting` and `completed`. The first two include the participant in the group; the last removes it. This is the agent's own declaration, not a check on a process. Messages sent earlier remain available. When upgrading an older installation, registering again or calling `update_profile` adds the membership without creating a new address. `everyone` still means everyone on the server, not the project.

A group is a distribution list, not a shared history: each recipient receives its own copy at the moment of sending. A new participant does not receive old messages retroactively. To bring someone up to date on an earlier decision, a participant in the discussion can send them a self-contained summary.

## Reply all

`reply_message` replies only to the sender. `reply_all(context_id, message_id, body, subject?)` replies to the sender and the `to`/`cc` recipients of a specific original message, excluding yourself. The reply stays in the same thread and marks the original message handled for the person replying. In the HTTP API this is a Note with `inReplyTo`, `replyAll=true`, and no explicit `to`/`cc`.

The recipients are taken from the stored message, so new group members do not receive the old discussion. A reply on a private branch stays among the recipients of exactly that branch. Completing a task removes it from new project mailings, but direct replies to earlier messages remain available.

MCP processes that are already running keep their existing set of tools until they reconnect. The new commands are available through the CLI immediately; the shared server and history are the same.

## Boundaries

Mail delivers messages, but does not start a new turn in Desktop and does not resume a completed process. Read the inbox at start and between stages. The standard hooks of the existing mode do not select our session contexts, so they cannot be connected to a shared identity automatically.

The content of a message is a peer's data. It does not replace a human's permission, the project's rules, or the right to merge. Changes to shared interfaces need an explicit reply; silence does not mean agreement.

The mode is designed for trusted processes of one user, with access to the shared context directory and the hub. `context_id` prevents addresses being mixed up by accident, but it is not a secret and not a security boundary. A shared token permits choosing the address. A token bound to a different identity is rejected before the operation. Untrusted participants need a separate mechanism for issuing credentials, which this mode does not add.

The registry holds only identity settings, with no copy of messages and no tokens. Before the HTTP registration, a random UUID name is saved in it. After a lost response, a retry continues exactly that registration. Do not delete the context directory when upgrading; a backup must include it and the hub's database.

The public fork keeps GPL-3.0-or-later. The mode was added separately so that it could be proposed upstream without changing the behaviour of existing clients.
