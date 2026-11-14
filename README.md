# roundcube-ai-backend

roundcube-ai-backend is a small backend for the [AI agent plugin for Roundcube](https://github.com/shimmi2/roundcube-ai-agent) by shimmi2. (Tomáš Šimek)
It implements the data API and the chat API that the plugin's `config.inc.php.dist` specifies.

The backend is a Python service that uses FastAPI.
It uses the OpenAI API for the model and Dovecot IMAP for the mailbox.
It is for small installations, from one mailbox to a few tens of mailboxes.

## Features

This version supports these plugin features:

- Login with the Roundcube username and password. Dovecot checks the password with an IMAP login.
- Settings -> AI: the switches, the instructions, the memory and the quota.
- Settings -> AI log: the task list and the monthly token usage.
- The chat. The agent can search, read, move and copy messages, and read attachments.
- AI reply, AI write and AI spell check.
- The client tools of the plugin. These open a message or fill the compose editor in the browser.

These features are not available yet:

- Sorting of incoming mail.
- Daily and weekly reports.

The plugin shows the switches for these features, but the backend does not run them.

## Requirements

- Python 3.11 or later. Debian 13 has Python 3.13.
- Dovecot with IMAP and a master user. The installation guide uses Dovecot 2.4.
- An OpenAI API key, or another server with an OpenAI-compatible API.
- An HTTPS reverse proxy, for example nginx.
- Roundcube 1.7 with the ai_agent plugin.

## Model providers

The backend is tested with the OpenAI API at `api.openai.com`.

The backend uses only the Chat Completions API. To use another server with an OpenAI-compatible API, set `[openai] base_url` to its URL. Examples are Ollama, vLLM, the llama.cpp server, LiteLLM and OpenRouter. These servers are not tested.

The server and the model must meet these conditions:

- The server supports tool calls (the `tools` field) in the OpenAI format.
- The model is good at tool use. The agent often makes several tool calls in a row, and small models often fail at this.
- The context window is large enough. If it is small, reduce `[agent] read_max_chars`.

A local server does not need an API key, but `[openai] api_key` must not be empty. Set it to any text, for example `"none"`.

Azure OpenAI needs an `api-version` parameter. The backend cannot send this parameter yet.

## Documentation

- [How to install the backend](docs/install.md)
- [Reference: the TOML file, the admin commands and the API behaviour](docs/reference.md)
- [About the design of the backend](docs/about.md)

## Development

Create a virtual environment and run the tests:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,pdf]'
.venv/bin/pytest
```

The tests replace IMAP and OpenAI with test doubles. You do not need a mail server or an API key to run them.

## Authors

- Rob Lister @listerr

The Roundcube plugin is by shimmi2. This project does not contain code from the plugin.

## Licence

roundcube-ai-backend is free software. You can distribute it and change it under the terms of the GNU General Public License, version 3 or (at your option) any later version. The plugin uses the same licence. The full text is in [LICENSE](LICENSE).
