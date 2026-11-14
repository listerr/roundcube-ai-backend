# How to install the backend

This guide installs roundcube-ai-backend on Debian 13 with Dovecot 2.4 and nginx.
At the end, Roundcube users can use the AI agent. The API is at `https://webmail.example.com/rcapi/`, on the same host name as the Roundcube webmail. Section 7 shows how to change `/rcapi/` to a random name.
Replace `webmail.example.com`, `example.com` and `rob@example.com` with your own names.

Run all commands as root on the host that runs the backend.

## 1. Install the code

1. Install the Python tools:

   ```sh
   apt install python3-venv git
   ```

2. Create a system user for the service:

   ```sh
   useradd --system --user-group --home-dir /opt/roundcube-ai-backend --shell /usr/sbin/nologin rcai
   ```

3. Get the code and install it in a virtual environment:

   ```sh
   git clone https://github.com/listerr/roundcube-ai-backend.git /opt/roundcube-ai-backend
   python3 -m venv /opt/roundcube-ai-backend/.venv
   /opt/roundcube-ai-backend/.venv/bin/pip install '/opt/roundcube-ai-backend[pdf]'
   ```

   The `pdf` extra installs `pypdf`, so that the agent can read PDF attachments.

## 2. Add a Dovecot master user

The agent reads the mailboxes with a master user. The master user logs in as `rob@example.com*rcai` with its own password.

1. Make a random password:

   ```sh
   tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40; echo
   ```

   Keep the password for `[imap] master_password` in section 3.

2. Make the hash of the password. The command asks for the password two times. Paste the password from step 1:

   ```sh
   doveadm pw -s BLF-CRYPT
   ```

   The command shows the hash, for example `{BLF-CRYPT}$2y$05$...`. `BLF-CRYPT` is the Dovecot name for bcrypt. Do not use the `-p` option, because other processes on the host can read a password on a command line.

3. Make the master user file. Replace `<hash>` with the hash from step 2, and keep the single quotes:

   ```sh
   install -d -o root -g dovecot -m 750 /etc/dovecot/auth
   install -o root -g dovecot -m 640 /dev/null /etc/dovecot/auth/master-users
   printf 'rcai:%s\n' '<hash>' > /etc/dovecot/auth/master-users
   ```

4. Copy the master passdb file to the Dovecot configuration directory:

   ```sh
   cp /opt/roundcube-ai-backend/deploy/rcai-dovecot-master.conf.ext /etc/dovecot/conf.d/
   ```

5. In `/etc/dovecot/conf.d/10-auth.conf` (or your equivalent), add an include line before the other `!include auth-*.conf.ext` lines. Dovecot runs the passdbs in the order of the configuration, so the master passdb must come first. For example:

   ```
   !include rcai-dovecot-master.conf.ext
   !include auth-sql.conf.ext
   ```

   You can also put the include line, or the contents of the file, elsewhere in your auth configuration. The master passdb must come before all other `passdb` blocks.

6. In the `allow_real_nets` line of `/etc/dovecot/conf.d/rcai-dovecot-master.conf.ext`, replace the example addresses with the addresses that the backend connects from. Keep `local`, so that `doveadm` can test the login. To find the addresses of the host, run:

   ```sh
   ip -br addr
   ```

   If `[imap] host` is the public host name of Dovecot, the backend connects from the public address of the host. If the backend runs on another host, use the addresses of that host.

7. Make sure that Dovecot accepts the configuration and that `rcai-master` is the first passdb. Then reload Dovecot and clear its auth cache:

   ```sh
   doveconf -n | grep -E '^passdb'
   systemctl reload dovecot
   doveadm auth cache flush
   ```

8. Test the master login for an existing mailbox and for a mailbox that does not exist. Each command asks for the master password:

   ```sh
   doveadm auth login 'rob@example.com*rcai'
   doveadm auth login 'nobody@example.com*rcai'
   ```

   The first command must succeed and show the userdb fields. The second command must fail in the userdb lookup. Do not use `doveadm auth test` for this check, because it does not do the userdb lookup.

   If the second command succeeds, stop. Do not use the backend, because the master user can then log in to any name. The comments in `deploy/rcai-dovecot-master.conf.ext` describe the cause. Run this test again after each change to your passdb or userdb configuration.

9. Test that the master password does not work from another address. Run this command on a different host, and log in with `a login "rob@example.com*rcai" "<master password>"`:

   ```sh
   openssl s_client -quiet -connect mail.example.com:993
   ```

   The login must fail with `NO [AUTHENTICATIONFAILED]`. The Dovecot log shows that the remote address is not allowed.

The file `deploy/rcai-dovecot-master.conf.ext` uses the Dovecot 2.4 syntax. Before you add `result_success = continue` to the block, read the comments in the file. For the reasons, read [How the master user is protected](about.md#how-the-master-user-is-protected).

## 3. Write the configuration

1. Copy the example and protect it. The file contains the master password and the API key:

   ```sh
   mkdir /etc/roundcube-ai-backend
   cp /opt/roundcube-ai-backend/config.example.toml /etc/roundcube-ai-backend/config.toml
   chown root:rcai /etc/roundcube-ai-backend/config.toml
   chmod 640 /etc/roundcube-ai-backend/config.toml
   ```

2. In `/etc/roundcube-ai-backend/config.toml`, set these keys:

   - `[imap] host`: a name that matches the TLS certificate of Dovecot.
   - `[imap] master_password`: the master password from section 2.
   - `[openai] api_key`: your OpenAI API key.
   - `[openai] model`: the name of a chat model that your account can use.
   - `[agent] timezone`: for example `Europe/London`.

   The [reference](reference.md#configuration-file) describes all keys.

3. Test the connections as the `rcai` user:

   ```sh
   sudo -u rcai /opt/roundcube-ai-backend/.venv/bin/roundcube-ai-admin check-imap rob@example.com
   sudo -u rcai /opt/roundcube-ai-backend/.venv/bin/roundcube-ai-admin check-openai
   ```

   Each command must print `OK`.

## 4. Start the service

1. Install and start the systemd unit:

   ```sh
   cp /opt/roundcube-ai-backend/deploy/roundcube-ai-backend.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now roundcube-ai-backend
   ```

   systemd creates `/var/lib/roundcube-ai-backend` for the database. Only the `rcai` user can read this directory, because the database contains conversations and mail text.

2. Make sure that the service answers:

   ```sh
   curl http://127.0.0.1:8085/rcapi/health
   ```

   The result is `{"ok":true,...}`.

## 5. Add the nginx location

Put the API on the same host name as your Roundcube webmail. On another host, the browser can block the chat requests. For the reason, read [Why the API is on the webmail host](about.md#why-the-api-is-on-the-webmail-host).

1. Add the login rate limit to the `http` block of nginx:

   ```sh
   cp /opt/roundcube-ai-backend/deploy/nginx-rcai-limit.conf /etc/nginx/conf.d/
   ```

   Debian includes `/etc/nginx/conf.d/*.conf` in the `http` block. If your `nginx.conf` does not, include the file in the `http` block yourself.
2. Copy the `location` block to the nginx snippets directory:

   ```sh
   cp /opt/roundcube-ai-backend/deploy/nginx-location.conf /etc/nginx/snippets/roundcube-ai-backend.conf
   ```

3. In the `server` block of your Roundcube webmail, for example `webmail.example.com`, add this line:

   ```nginx
   include snippets/roundcube-ai-backend.conf;
   ```

4. Make sure that nginx accepts the configuration, then reload it:

   ```sh
   nginx -t && systemctl reload nginx
   ```

5. Test the public URL:

   ```sh
   curl https://webmail.example.com/rcapi/health
   ```

## 6. Connect Roundcube

1. Install the ai_agent plugin in Roundcube, as its README describes.
2. In `plugins/ai_agent/config.inc.php` in the Roundcube directory, set the API URL. Use `deploy/roundcube-plugin-config.php` as an example:

   ```php
   $config['ai_agent_api_url'] = 'https://webmail.example.com/rcapi/';
   $config['ai_agent_chat_url'] = null;
   ```

3. Reload Roundcube in the browser. Then open **AI agent** in the main menu. It shows a message that AI is not enabled, until you do section 8.

## 7. Use a random sub-URL (optional)

You may like to use a random sub-URL for the API. Swap the default `/rcapi/` to a random string, for example `/gkhU4CHkag9I/`. A random name makes the API more difficult to find for scanners and for guessing.

The sub-URL is in three files:

- `[server] base_path` in `/etc/roundcube-ai-backend/config.toml`
- the `location` line in `/etc/nginx/snippets/roundcube-ai-backend.conf`
- `ai_agent_api_url` in `plugins/ai_agent/config.inc.php` in the Roundcube directory

1. Make a random name and keep it in a shell variable:

   ```sh
   EP=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 12); echo "$EP"
   ```

2. Replace `/rcapi` in the three files. Replace `/var/www/roundcube` with your Roundcube directory:

   ```sh
   sed -i "s#^base_path = \"/rcapi\"#base_path = \"/$EP\"#" /etc/roundcube-ai-backend/config.toml
   sed -i "s#/rcapi/#/$EP/#" /etc/nginx/snippets/roundcube-ai-backend.conf
   sed -i "s#/rcapi/#/$EP/#" /var/www/roundcube/plugins/ai_agent/config.inc.php
   ```

3. Make sure that each file has the new name:

   ```sh
   grep -n "$EP" /etc/roundcube-ai-backend/config.toml /etc/nginx/snippets/roundcube-ai-backend.conf /var/www/roundcube/plugins/ai_agent/config.inc.php
   ```

   The plugin line is then, for example:

   ```php
   $config['ai_agent_api_url'] = 'https://webmail.example.com/gkhU4CHkag9I/';
   ```

4. Restart the backend and reload nginx:

   ```sh
   systemctl restart roundcube-ai-backend
   nginx -t && systemctl reload nginx
   ```

5. Test the new URL and the old URL:

   ```sh
   curl https://webmail.example.com/$EP/health
   curl https://webmail.example.com/rcapi/health
   ```

   The first command must show `{"ok":true,...}`. The second command must not show it.

6. Reload Roundcube in the browser, so that it uses the new URL.

## 8. Enable AI for a mailbox

The AI is off until you enable the domain and the mailbox. The agent sends the text of the messages that it reads to OpenAI. Before you enable a mailbox, make sure that your data protection rules permit this.

Enable the domain and the mailbox:

```sh
sudo -u rcai /opt/roundcube-ai-backend/.venv/bin/roundcube-ai-admin domain enable example.com
sudo -u rcai /opt/roundcube-ai-backend/.venv/bin/roundcube-ai-admin mailbox enable rob@example.com
```

To turn AI on for all mailboxes at a domain, you can set `[access] enable_domains` in the configuration file instead. Then restart the service. See the [`[access]` reference](reference.md#access).

Log in to Roundcube as `rob@example.com` and open **AI agent** in the main menu.

## If something does not work

- Read the service log: `journalctl -u roundcube-ai-backend -f`.
- If Roundcube shows "The mail server is not available.", the backend cannot connect to Dovecot. Run `roundcube-ai-admin check-imap` again.
- If the chat shows "The AI service did not accept the backend's API key.", check `[openai] api_key`.
- If the chat shows "Connection to the assistant was lost.", open the browser console. If it shows a `connect-src` error, the API is on another host than the webmail. Move the nginx location to the webmail host, as section 5 describes.
- If nginx refuses to start with `zero size shared memory zone "rcai_login"`, nginx did not read `nginx-rcai-limit.conf`. Make sure that the file is in `/etc/nginx/conf.d/` and that the `http` block includes it.
- If the chat answer arrives only after a long time, or never, make sure that `proxy_buffering off` is in the nginx location.

## Update an existing installation

1. Get the new code and install it:

   ```sh
   git -C /opt/roundcube-ai-backend pull
   /opt/roundcube-ai-backend/.venv/bin/pip install '/opt/roundcube-ai-backend[pdf]'
   ```

2. Compare `/opt/roundcube-ai-backend/config.example.toml` with your `config.toml`. The backend refuses unknown keys and values outside the permitted range. The [reference](reference.md#configuration-file) gives each range.

3. Compare the systemd unit and the nginx files in `/opt/roundcube-ai-backend/deploy/` with your copies. Make the same changes to your copies. Do not copy `nginx-location.conf` over your snippet, because the snippet contains your sub-URL.

4. Make sure that only the `rcai` user can read the database directory and files:

   ```sh
   chmod 700 /var/lib/roundcube-ai-backend
   chmod 600 /var/lib/roundcube-ai-backend/rcai.db*
   ```

5. Restart the service:

   ```sh
   systemctl daemon-reload
   systemctl restart roundcube-ai-backend
   journalctl -u roundcube-ai-backend -n 20
   ```

   If the log shows a configuration error, correct `config.toml` and restart again.
