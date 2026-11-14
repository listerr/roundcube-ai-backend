# Reference

## Configuration file

The backend reads one TOML file. The `--config` option sets the path. Without the option, the backend uses the environment variable `RCAI_CONFIG`, then `/etc/roundcube-ai-backend/config.toml`.

An unknown section or key stops the backend with an error. A number outside its range also stops the backend. The "Range" column gives the permitted values.

### [server]

| Key | Default | Range | Description |
|---|---|---|---|
| `host` | `"127.0.0.1"` | | Address that uvicorn listens on. |
| `port` | `8085` | 1 to 65535 | Port that uvicorn listens on. |
| `base_path` | `"/rcapi"` | | Sub-URL of the reverse proxy. Each route works with and without this prefix. |
| `cors_origins` | `[]` | | Origins of other hosts that browsers can call the API from. Empty means the same origin only. |
| `forwarded_allow_ips` | `"127.0.0.1"` | | Proxy addresses whose `X-Forwarded-*` headers the backend accepts. The login rate limit uses the client address from these headers. |
| `log_level` | `"info"` | | `debug`, `info`, `warning` or `error`. |
| `max_body_bytes` | `2000000` | 10000 to 50000000 | Largest request body. A larger body gets HTTP 413. |
| `allow_insecure_transport` | `false` | | Permits `[imap] security = "none"`, `[imap] tls_verify = false` and an `http://` value of `[openai] base_url`. Loopback hosts do not need it. If it is `true`, the backend logs a warning at the start. |

### [database]

| Key | Default | Description |
|---|---|---|
| `path` | `"/var/lib/roundcube-ai-backend/rcai.db"` | SQLite database file. The backend creates it. |

### [imap]

| Key | Default | Range | Description |
|---|---|---|---|
| `host` | `"localhost"` | | Dovecot host. |
| `port` | `993` | 1 to 65535 | IMAP port. |
| `security` | `"ssl"` | | `ssl` (implicit TLS), `starttls` or `none`. For a host that is not a loopback address, `none` needs `[server] allow_insecure_transport`. |
| `tls_verify` | `true` | | If `true`, the backend makes sure that the certificate is valid and matches the host name. For a host that is not a loopback address, `false` needs `[server] allow_insecure_transport`. |
| `timeout` | `30` | 1 to 600 | Network timeout in seconds. |
| `master_user` | `""` | | Dovecot master user. Required. |
| `master_password` | `""` | | Password of the master user. Required. |
| `master_separator` | `"*"` | | Dovecot `auth_master_user_separator`. |

### [auth]

| Key | Default | Range | Description |
|---|---|---|---|
| `session_ttl` | `28800` | 300 to 2592000 | Lifetime of a login token in seconds. |
| `max_failures` | `5` | 1 to 100 | Failed logins for one login name from one client address before HTTP 429. |
| `max_failures_ip` | `20` | 1 to 10000 | Failed logins for all login names from one client address before HTTP 429. |
| `failure_window` | `300` | 10 to 86400 | Time window for `max_failures` and `max_failures_ip`, in seconds. |
| `max_concurrent_logins` | `4` | 1 to 100 | Password checks at the same time. More logins get HTTP 429. |
| `default_domain` | `""` | | Domain for logins without `@`. The address becomes `login@default_domain`. |

### [access]

| Key | Default | Description |
|---|---|---|
| `enable_all` | `false` | If `true`, AI is on for all domains and mailboxes by default. |
| `enable_domains` | `[]` | Domains where AI is on by default, for the domain and each of its mailboxes. Upper and lower case are the same. |

These keys set the default only. A setting that you make with `roundcube-ai-admin` overrides the default. See [roundcube-ai-admin](#roundcube-ai-admin).

### [openai]

| Key | Default | Range | Description |
|---|---|---|---|
| `api_key` | `""` | | OpenAI API key. Empty means the environment variable `OPENAI_API_KEY`. Required. |
| `base_url` | `"https://api.openai.com/v1"` | | Any OpenAI-compatible Chat Completions endpoint. For a host that is not a loopback address, `http://` needs `[server] allow_insecure_transport`. |
| `model` | `""` | | Model name. Required. |
| `timeout` | `300` | 5 to 3600 | Timeout of one model request, in seconds. |
| `params` | `{}` | | Extra parameters for each request, for example `{reasoning_effort = "low"}`. |

### [agent]

| Key | Default | Range | Description |
|---|---|---|---|
| `max_steps` | `15` | 1 to 100 | Model requests for one user message. The last step cannot call tools. |
| `max_concurrent` | `4` | 1 to 1000 | Agent runs at the same time. More requests get HTTP 429 with `Retry-After: 30`. |
| `max_per_mailbox` | `1` | 1 to 100 | Agent runs at the same time for one mailbox. More requests get HTTP 429 with `Retry-After: 10`. |
| `run_timeout` | `600` | 10 to 7200 | Seconds for one agent run, all steps together. Then the agent stops with an error. |
| `daily_tokens` | `0` | 0 to 10000000000 | Tokens for one mailbox per day, input and output together. Then HTTP 429 until midnight, server time. `0` means no limit. |
| `max_turns` | `400` | 10 to 100000 | Stored turns in one conversation. Then HTTP 400. |
| `max_tools` | `32` | 0 to 128 | Client tools in one request. |
| `max_tool_bytes` | `32768` | 1000 to 1000000 | Size of all client tool definitions in one request, as JSON. |
| `ping_interval` | `10` | 0.01 to 60 | Seconds between `: ping` lines in the chat stream. |
| `read_max_chars` | `30000` | 1000 to 1000000 | Characters of one message or attachment that the model gets. |
| `search_limit` | `100` | 0 to 100000 | Maximum messages in one search result. The model asks for 20 messages if it does not give a number. `0` means no limit. Each message in the result goes to the model, so a high value can use many tokens. |
| `timezone` | `""` | | Time zone of the date in the system prompt. Empty means the server time zone. |

## Commands

### roundcube-ai-backend

Starts the API server. It runs one process. Do not start more workers, because the agent runs and the login rate limit are kept in memory.

```
roundcube-ai-backend [--config FILE]
```

### roundcube-ai-admin

Changes the database and tests the connections. Run it as the service user, so that the database file keeps the correct owner.

| Command | Description |
|---|---|
| `domain list` | Lists the domains that have a setting or are in `enable_domains`, with their AI state. The `SET BY` column shows `admin` or `config`. |
| `domain enable DOMAIN` | Enables AI for a domain. The plugin calls this "purchased". |
| `domain disable DOMAIN` | Disables AI for a domain. |
| `domain default DOMAIN` | Removes the setting of a domain. The `[access]` default applies again. |
| `mailbox list` | Lists the mailboxes that logged in or that an administrator added. The `SET BY` column shows `admin` or `config`. |
| `mailbox enable LOGIN` | Enables AI for a mailbox. If the mailbox does not exist, the command adds it. |
| `mailbox disable LOGIN` | Disables AI for a mailbox. |
| `mailbox default LOGIN` | Removes the setting of a mailbox. The `[access]` default applies again. |
| `check-imap LOGIN` | Logs in as the master user for `LOGIN`, then shows the folders and the quota. |
| `check-openai` | Sends one short request to the model and shows the answer and the token count. |

If both the domain and the mailbox are enabled, the user can use the AI. Otherwise the AI is off.

For each domain and each mailbox, the backend uses the first value that it finds:

1. The setting from `enable` or `disable`.
2. `true`, if `[access] enable_all` is `true` or the domain is in `[access] enable_domains`.
3. `false`.

## HTTP routes

| Route | Description |
|---|---|
| `GET\|POST <base>/?fn=<operation>` | Data API. |
| `POST <base>/v1/chat/completions` | Chat API. |
| `GET <base>/health` | Returns `{"ok": true, "version": ..., "active": N}`. No authentication. |

The plugin's `config.inc.php.dist` specifies the data API and the chat API. This section lists only what the backend adds to that specification, or what it decides where the specification leaves a choice.

### Data API

| Operation | Behaviour of this backend |
|---|---|
| `do_login` | Tries an IMAP login to Dovecot with the password. If Dovecot is not reachable, returns 503. Returns 429 after too many failed logins (`max_failures`, `max_failures_ip`) or if `max_concurrent_logins` password checks are running. Stores the login in lower case. Ignores `persist`. |
| `do_logout` | Deletes the token. Always returns `{"ok": true}`. |
| `mail_get_mailbox` | Reads the quota with the IMAP `GETQUOTAROOT` command, at most once in 60 seconds for each mailbox. If Dovecot has no quota, the values are `null`. The report times are always `null` in this version. |
| `mail_store_mailbox` | If the domain is not enabled, returns 403 for an AI field. Returns 403 for an unknown field. If the text is the same as the default, stores an empty value. |
| `ai_list_tasks` | `limit` is 1 to 1000 (default 50). `type=chat` lists `live`, `reply` and `create` tasks with stored turns, by last activity. |
| `ai_get_conversation` | Returns the user messages, the answers and the client tool calls with their results. Steps with server tools only are not returned. |
| `ai_usage_months` | `months` is 1 to 120 (default 12). |

A request body larger than `[server] max_body_bytes` gets HTTP 413, for the data API and for the chat API.

### Chat API

- Each model step is a normal request to OpenAI, not a streaming request. The answer arrives at the end, in chunks of 200 characters.
- When the client closes the connection (the stop button), the agent stops. The task gets the state `done` and the error `stopped`.
- A tool call without a result gets the result `ERROR: no result, the call was interrupted` before the next model request.
- A client tool cannot have the name of a server tool. The backend returns 400.
- A tool name has 1 to 64 letters, digits, `_` or `-`. Other names get HTTP 400.
- A request has at most 1000 messages.

### Server tools

| Tool | Chat types | Description |
|---|---|---|
| `mail_list_folders` | live, reply, create | Folders with special use, and optional message counts. |
| `mail_search` | live, reply, create | IMAP `SEARCH` in one folder or in all folders. |
| `mail_read` | live, reply, create | Headers, text and attachment list. Does not set `\Seen`. |
| `mail_read_attachment` | live, reply, create | Text of a text, HTML, message or PDF attachment. |
| `mail_move` | live | Moves a message. |
| `mail_copy` | live | Copies a message. |
| `mail_create_folder` | live | Creates and subscribes a folder. |
| `memory_update` | live | Replaces the memory of the user. Maximum 20,000 characters. |

The `spellcheck` chat type has no server tools.

## Database

The SQLite file has these tables:

| Table | Contents |
|---|---|
| `domain` | Domain names and their AI state. A domain without a row uses the `[access]` default. |
| `mailbox` | One row per login: address, full name, switches, instructions, memory. If `ai_enable` is NULL, the mailbox uses the `[access]` default. |
| `session` | SHA-256 hashes of login tokens, with the expiry time. |
| `task` | One row per conversation or task: type, state, times, token counts, result, error. |
| `turn` | The messages of each conversation, including tool calls and tool results. |
