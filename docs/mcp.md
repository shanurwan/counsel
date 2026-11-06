# The MCP server

`counsel-mcp` puts Counsel's journal and handbook in the tools people already use.
It runs as a local stdio process per client, so nothing new is hosted.

| Tool | What it does | Writes |
|---|---|---|
| `list_agents` | Owner, tier, capabilities, triggers and suite for each agent | no |
| `privilege_matrix` | What each agent's role can do right now, asked of the live database | no |
| `list_runs` | Recent runs with status and cost | no |
| `replay_run` | One run in full: steps, prompts, responses, audit entries | no |
| `refused_writes` | Forbidden writes Postgres refused, and rollbacks | no |
| `value_ledger` | Accepted outputs, hours absorbed and spend, per agent and month | no |
| `pending_reviews` | Drafts waiting for a person | no |
| `ask_handbook` | Queues a question; at most 20 an hour | a question |
| `handbook_answer` | The latest answer to that exact question | no |

## The tool list is not the policy

The server logs in as `counsel_mcp`, which can read the journal and insert a
question, and nothing else. A bug here, or a hijacked client, still cannot publish
a draft, carry a note, send a message or touch a commitment, because Postgres
refuses. `counsel audit` fails if that login is ever granted more, and a test grants
it `UPDATE` to prove the audit notices.

Publishing is deliberately missing. `counsel review` runs on a person's own
connection, so `reviewed_by` is that person. A publish tool would make the reviewer
"whoever holds the MCP login", and the human gate would stop identifying a human.

## Choices

- **`ask_handbook` is asynchronous.** The agent runs on the next tick, up to 30
  minutes away. The tool says so, and `handbook_answer` fetches the result. Blocking
  a client for that long would break most of them, and running the agent inside the
  server would give this login the agent's privileges.
- **Results are data.** A replay holds ticket and issue text, including injection
  attempts. Tool descriptions say so and results are JSON, but a client's model
  still reads them, so point this server only at clients you would show the journal
  to.
- **The rate limit is in the server, not the database.** A client looping on a
  question costs model calls. This is a cost guard, not a security boundary: anyone
  with the login's password could insert directly, and the worst outcome is spend.
- **Tools declare `readOnlyHint`,** so a client can skip the confirmation prompt on
  reads and keep it for `ask_handbook`.

## Run it

```sh
pip install "counsel[mcp]"
psql "$ADMIN_URL" -c "alter role counsel_mcp password '...'"
export COUNSEL_MCP_URL=postgresql://counsel_mcp:...@host:5432/postgres
counsel-mcp
```

A client configuration, for example in Claude Desktop:

```json
{
  "mcpServers": {
    "counsel": {
      "command": "counsel-mcp",
      "env": { "COUNSEL_MCP_URL": "postgresql://counsel_mcp:...@host:5432/postgres" }
    }
  }
}
```

The login holds at most 3 connections and has a 10 s statement timeout, so a busy
client cannot starve the agents.
