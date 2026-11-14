# TODO

## Features

- [ ] Sort incoming mail. The plugin has the switches, but the backend does not run them. A possible design is one IMAP `IDLE` connection for each enabled mailbox.
- [ ] Investigate using decision models for mailbox sorting/automations instead of full model.
- [ ] Investigate allowing AI Agent to mark messages as read/unread, so an instruction like "find all messages from ... and mark them as read" works.
- [ ] Allow it to create/update sieve rules via managesieve?
- [ ] Send daily and weekly reports. `mail_get_mailbox` returns `null` for the report times until this feature exists.
- [ ] Send the `api-version` parameter, so that the backend can use Azure OpenAI.
- [ ] Stream partial answers to the browser. At the moment, the answer arrives at the end.
- [ ] Add delete feature to plugin to delete past conversations?
- [ ] Add copy feature to plugin to copy chat responses?

## Maintenance

- [ ] Delete old tasks and turns after a configurable number of days. At the moment, the database keeps them for ever.
- [ ] Add a `roundcube-ai-admin` command that deletes the data of one mailbox.
- [ ] Test the backend with other OpenAI-compatible servers, for example Ollama and vLLM.

## Release

- [ ] Make the first git commit and publish the repository.
- [ ] Tag version 0.1.0.
