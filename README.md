# lr-mcp

An unofficial MCP server for **Adobe Photoshop Lightroom** — the cloud-based Lightroom app, not Lightroom Classic. It talks to the [Lightroom Partner API](https://developer.adobe.com/lightroom/lightroom-api-docs/) (`https://lr.adobe.io`) with your own Adobe credentials.

> This project is not affiliated with, endorsed by, or sponsored by Adobe. Adobe, Photoshop and Lightroom are either registered trademarks or trademarks of Adobe in the United States and/or other countries.

## Tools

| Tool | What it does |
|---|---|
| `lr_login` | Browser OAuth sign-in (only needed when tokens are missing or expired) |
| `lr_account` | Subscription status and cloud storage usage |
| `lr_list_albums` | Search albums by path and get their ids |
| `lr_list_photos` | Filter by album, capture date, time of day, rating, pick/reject flag, camera, lens, aperture, ISO, file name |
| `lr_get_photo` | Capture info plus a summary of the photo's edit settings (XMP) |
| `lr_view_photos` | Return edited JPEG renditions as images the model can see (up to 12) |
| `lr_download_photos` | Save 640 – 2560 px or full-size JPEGs to disk |
| `lr_create_project_album` / `lr_add_to_project_album` / `lr_delete_project_album` | Manage project albums owned by this server |

### Limits of the Partner API

- Edits (develop settings) and ratings of existing photos are read-only.
- Photos cannot be deleted.
- Albums you created in Lightroom cannot be modified; only project albums created through the API can.
- Preset contents cannot be read.

## Setup

Setup takes about ten minutes. Steps marked **[human]** need a person at a browser; an AI agent can do the rest (see [For AI agents](#for-ai-agents)).

### Prerequisites

- An Adobe account with a Lightroom plan or trial.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`uv --version`). It installs Python 3.12+ itself if needed.
- `openssl` (`openssl version`). Preinstalled on macOS and most Linux distributions.

### 0. Install

```bash
uv tool install lr-mcp
lr-mcp doctor
```

This puts an `lr-mcp` command in `~/.local/bin` (run `uv tool update-shell` and open a new terminal if the shell cannot find it). All later steps use it. Update with `uv tool upgrade lr-mcp`.

`doctor` checks the setup in order and prints the first thing to fix.

### 1. Create an Adobe credential [human]

Each user needs their own credential: until Adobe reviews an integration it accepts only 25 beta users, so a shared client ID is not practical.

1. Sign in to the [Adobe Developer Console](https://developer.adobe.com/console) and click **Create new project**.
2. In the project, click **Add API**, choose **Lightroom Services**, then pick the **OAuth Web App** credential type.
3. Set **Default redirect URI** to `https://localhost:8000/callback` and **Redirect URI pattern** to `https://localhost:[0-9]+/callback`, then save. The pattern allows any port, so sign-in can use another one when 8000 is busy. A changed pattern can take a few minutes to apply.
4. Open **Lightroom Services** in the project's left menu, click **Manage beta users**, and add the email of the Adobe account you will sign in with. Without this, the API refuses the account.
5. Open **Credentials → OAuth Web App** and click **Download** to save the credential as JSON. It contains the client secret; step 2 reads it and can delete it.

### 2. Save the credential

```bash
lr-mcp setup --from ~/Downloads/<credential>.json --delete
```

This reads the client ID and secret from the downloaded file (the project JSON from the project overview's **Download** also works) and writes `ADOBE_CLIENT_ID` and `ADOBE_CLIENT_SECRET` to `.env` in the state directory, which `doctor` prints on its first line (`~/.config/lr-mcp/` by default). The file gets mode 600 and keeps any other lines. Only the first characters of the client ID are printed. `--delete` removes the downloaded file afterwards; without it, delete the file yourself.

| Variable | Required | Description |
|---|---|---|
| `ADOBE_CLIENT_ID` | yes | Client ID (also called API key) |
| `ADOBE_CLIENT_SECRET` | yes | Client secret |
| `LR_MCP_PORT` | no | Sign-in callback port. Default `8000`; if it is busy and this is not set, sign-in picks a free port. |

To enter the values by hand instead, put `ADOBE_CLIENT_ID=…` and `ADOBE_CLIENT_SECRET=…` lines in `.env` and `chmod 600` it. Environment variables with the same names override the file, but keep the client secret in `.env`: an MCP client's `"env"` / `-e` stores it in plain text in the client's config. Variables exported only in your shell are not seen by an MCP client started from the GUI, so put settings in `.env`.

To keep the state somewhere else, set the environment variable `LR_MCP_HOME` to a directory. It cannot go in `.env` (it says where `.env` is), so it must also be set for `lr-mcp login` and in the MCP client config (`"env"` in step 5). If `XDG_CONFIG_HOME` is set, the default is `$XDG_CONFIG_HOME/lr-mcp`; if it is set only in your shell, the MCP client will not see it, so set `LR_MCP_HOME` in the client config to the directory `doctor` prints.

### 3. Sign in [human]

```bash
lr-mcp login
```

A browser window opens on the Adobe sign-in page. Sign in with the account added as a beta user and allow access. Adobe then redirects to `https://localhost:<port>/callback` (8000 unless it was busy), which uses a self-signed certificate, so the browser shows a certificate warning: choose **Advanced → Proceed to localhost** (Chrome) or **Show Details → visit this website** (Safari). The page then says "Signed in" and the terminal prints where the tokens were saved. Tokens are stored with mode 600 and refreshed automatically.

### 4. Check

```bash
lr-mcp doctor
```

Every line should start with `[ok]`, ending in `Catalog found. Setup is complete.` Otherwise it prints a `fix:` for the first problem and exits with status 1.

### 5. Add to your MCP client

**Claude Desktop.** Edit the config file (create it if missing):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add an entry under `mcpServers`, keeping any existing ones (if a `lightroom` entry already exists, check what it runs and whether it sets `"env"` before replacing it). Claude Desktop does not read your shell's `PATH`, so use the absolute path printed by `which lr-mcp`:

```json
{
  "mcpServers": {
    "lightroom": {
      "command": "/Users/<you>/.local/bin/lr-mcp"
    }
  }
}
```

If you set `LR_MCP_HOME`, add `"env": {"LR_MCP_HOME": "/path/to/dir"}` to the entry. Quit Claude Desktop completely and reopen it; the `lr_*` tools then appear in the tools menu. On macOS the server log is `~/Library/Logs/Claude/mcp-server-lightroom.log`.

**Claude Code:**

```bash
claude mcp add lightroom -s user -- "$(which lr-mcp)"
```

The absolute path avoids depending on the `PATH` Claude Code was started with; check that `which lr-mcp` prints a path first, or an empty command gets registered. If `claude mcp get lightroom` shows an existing server, check what it runs and its environment, then remove it with `claude mcp remove lightroom -s user` before adding. Start a new Claude Code session for the `lr_*` tools to appear. If you set `LR_MCP_HOME`, add `-e LR_MCP_HOME=/path/to/dir` before `--`.

The client config can be added before or after signing in; without tokens the tools reply "Not signed in" and `lr_login` can do the sign-in from the chat.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `lr-mcp: command not found` | Run `uv tool update-shell` and open a new terminal, or call `~/.local/bin/lr-mcp` directly. |
| `doctor`: `Callback port … (LR_MCP_PORT) is in use` | Change `LR_MCP_PORT` in `.env` to a free port, or remove it so sign-in picks one. |
| `doctor`: `Not set: ADOBE_CLIENT_ID…` | Step 2: run `lr-mcp setup --from <credential>.json`. |
| `setup`: `The file has 2 credentials` | Download only the OAuth Web App credential (step 1.5) instead of the whole project, and retry. |
| The Adobe sign-in page shows an error right away | Usually a wrong `ADOBE_CLIENT_ID`: `doctor` cannot verify it before sign-in. Run step 2 again with the right credential JSON. |
| `doctor`: `rejected the client id` | `ADOBE_CLIENT_ID` is wrong or the tokens belong to another credential. Run step 2 again with the right credential JSON, then sign in again. |
| `doctor`: `Lightroom API refused the account` | The signed-in account is not a beta user (step 1.4) or has no Lightroom plan. |
| The browser lands on `https://localhost:8000/callback` although sign-in used another port | Adobe silently falls back to the default redirect URI when the requested one does not match the pattern. Set the pattern to `https://localhost:[0-9]+/callback` (step 1.3; `\d` is not supported). Credentials set up with `https://localhost:8000/.*` need this change. |
| The sign-in redirect shows another app's page or never loads | Something else listens on the port (`lsof -iTCP:<port> -sTCP:LISTEN`). Stop it, or set `LR_MCP_PORT` to a free port. |
| `login` times out after 5 minutes | The browser stopped at the certificate warning; proceed past it next time. |
| `Token refresh failed` | Run `lr-mcp login` again. |
| Tools missing in Claude Desktop | Use the absolute path to `lr-mcp`, fully restart the app, and read the log file above. |

## For AI agents

If a user asks you to set this server up:

- **Never ask the user to paste the client secret into the chat, and never read or print the downloaded credential JSON or the contents of the state directory** (`.env`, `.tokens.json`, `.certs/`, editor backups such as `.env~`). Save the credential with `lr-mcp setup --from <file> --delete` (step 2). Only pass the file's path; listing file names, e.g. `ls -t ~/Downloads/*.json`, is fine. If more than one file could be it, ask the user which. `setup` replaces only the `ADOBE_CLIENT_*` lines in `.env`, so it is safe to run on an existing file.
- Install (step 0) and run `lr-mcp doctor` first. If it already prints `Setup is complete.`, skip to step 5. Run `doctor` again after every step; a `fix:` that starts with `[human]` is something to hand to the user.
- The user must create the credential, add the beta user, download the credential JSON, and finish the browser sign-in. Give them the exact console steps from step 1; the redirect URI values are the same for everyone.
- Put only `LR_MCP_HOME` or `LR_MCP_PORT` in a client's `"env"` / `-e`, never `ADOBE_CLIENT_ID` or `ADOBE_CLIENT_SECRET`.
- `lr-mcp login` blocks for up to 5 minutes while the user signs in; the `[human]` part is the browser sign-in. You may run it in the background, but first tell the user a browser window will open on their screen and warn them about the certificate page. Run `doctor` afterwards to confirm the sign-in.
- Setup is done when `doctor` prints `Catalog found. Setup is complete.` Add the client config (step 5) before or after that. If a `lightroom` entry already exists in the client, show the user what it runs and ask before replacing it. When reading `claude mcp get` output or `claude_desktop_config.json`, show only commands and env variable names, never env values: they may hold other servers' secrets. For Claude Desktop, merge into the existing JSON instead of overwriting it, use the absolute path from `which lr-mcp`, pass `LR_MCP_HOME` in `"env"` if it is set, and tell the user to quit and reopen the app. For Claude Code, register the absolute path and tell the user to start a new session.

## Development

```bash
git clone https://github.com/longfin/lr-mcp && cd lr-mcp
uv sync
uv run lr-mcp doctor
uv run pytest
```

To run a checkout from an MCP client, use `uv run --directory /path/to/lr-mcp lr-mcp` as the command. Without installing, `uvx lr-mcp <command>` also works.

## License

[MIT](LICENSE)
