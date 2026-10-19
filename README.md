# lr-mcp

An unofficial MCP server for **Adobe Photoshop Lightroom** — the cloud-based Lightroom app, not Lightroom Classic. It talks to the [Lightroom Partner API](https://developer.adobe.com/lightroom/lightroom-api-docs/) (`https://lr.adobe.io`) with your own Adobe credentials.

> This project is not affiliated with, endorsed by, or sponsored by Adobe. Adobe, Photoshop and Lightroom are either registered trademarks or trademarks of Adobe in the United States and/or other countries.

## Tools

| Tool | What it does |
|---|---|
| `lr_login` | Browser OAuth sign-in (only needed when tokens are missing or expired) |
| `lr_account` | Subscription status and cloud storage usage |
| `lr_list_albums` | Search albums by path and get their ids |
| `lr_list_photos` | Filter by album, capture date, rating, pick/reject flag, camera, lens, ISO, file name |
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
uv tool install git+https://github.com/longfin/lr-mcp
lr-mcp doctor
```

This puts an `lr-mcp` command in `~/.local/bin` (run `uv tool update-shell` and open a new terminal if the shell cannot find it). All later steps use it. Update with `uv tool upgrade lr-mcp`.

`doctor` checks the setup in order and prints the first thing to fix. Before you start step 1, it confirms that the sign-in callback port (8000 by default) is free; if it is not, choose another port now (see step 2) so the redirect URI you enter in step 1 matches.

### 1. Create an Adobe credential [human]

Each user needs their own credential: until Adobe reviews an integration it accepts only 25 beta users, so a shared client ID is not practical.

1. Sign in to the [Adobe Developer Console](https://developer.adobe.com/console) and click **Create new project**.
2. In the project, click **Add API**, choose **Lightroom Services**, then pick the **OAuth Web App** credential type.
3. Set **Default redirect URI** to `https://localhost:8000/callback` and **Redirect URI pattern** to `https://localhost:8000/.*`, then save. (If you chose another port in step 0, use it here instead of 8000.)
4. Open **Lightroom Services** in the project's left menu, click **Manage beta users**, and add the email of the Adobe account you will sign in with. Without this, the API refuses the account.
5. Open **Credentials → OAuth Web App** and keep the **Client ID** and **Client Secret** at hand for step 2.

### 2. Save the credential

Settings live in a `.env` file in the state directory, which `doctor` prints on its first line (`~/.config/lr-mcp/` by default). Environment variables with the same names override the file.

| Variable | Required | Description |
|---|---|---|
| `ADOBE_CLIENT_ID` | yes | Client ID (also called API key) |
| `ADOBE_CLIENT_SECRET` | yes | Client secret |
| `LR_MCP_PORT` | no | Sign-in callback port, default `8000` |

Create the file, then open it in an editor and paste the two values after the `=` signs (add `LR_MCP_PORT=<port>` if you are not using 8000):

```bash
mkdir -p ~/.config/lr-mcp
printf 'ADOBE_CLIENT_ID=\nADOBE_CLIENT_SECRET=\n' > ~/.config/lr-mcp/.env
chmod 600 ~/.config/lr-mcp/.env
open -e ~/.config/lr-mcp/.env      # macOS; on Linux use any editor, e.g. nano
```

To keep the state somewhere else, set the environment variable `LR_MCP_HOME` to a directory. It cannot go in `.env` (it says where `.env` is), so it must also be set for `lr-mcp login` and in the MCP client config (`"env"` in step 5). If `XDG_CONFIG_HOME` is set, the default is `$XDG_CONFIG_HOME/lr-mcp`.

### 3. Sign in [human]

```bash
lr-mcp login
```

A browser window opens on the Adobe sign-in page. Sign in with the account added as a beta user and allow access. Adobe then redirects to `https://localhost:8000/callback`, which uses a self-signed certificate, so the browser shows a certificate warning: choose **Advanced → Proceed to localhost** (Chrome) or **Show Details → visit this website** (Safari). The page then says "Signed in" and the terminal prints where the tokens were saved. Tokens are stored with mode 600 and refreshed automatically.

### 4. Check

```bash
lr-mcp doctor
```

Every line should start with `[ok]`, ending in `Catalog found. Setup is complete.` Otherwise it prints a `fix:` for the first problem and exits with status 1.

### 5. Add to your MCP client

**Claude Desktop.** Edit the config file (create it if missing):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add an entry under `mcpServers`, keeping any existing ones (if a `lightroom` entry already exists, replace it). Claude Desktop does not read your shell's `PATH`, so use the absolute path printed by `which lr-mcp`:

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
claude mcp add lightroom -s user -- lr-mcp
```

The client config can be added before or after signing in; without tokens the tools reply "Not signed in" and `lr_login` can do the sign-in from the chat.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `lr-mcp: command not found` | Run `uv tool update-shell` and open a new terminal, or call `~/.local/bin/lr-mcp` directly. |
| `doctor`: `Callback port … is in use` | Put `LR_MCP_PORT=<free port>` in `.env` and use that port in the console's redirect URI. |
| `doctor`: `Not set: ADOBE_CLIENT_ID…` | Step 2: fill in `.env` in the state directory. |
| The Adobe sign-in page shows an error right away | Usually a wrong `ADOBE_CLIENT_ID`: `doctor` cannot verify it before sign-in. Copy it again from the console. |
| `doctor`: `rejected the client id` | `ADOBE_CLIENT_ID` is wrong or the tokens belong to another credential. Fix `.env`, then sign in again. |
| `doctor`: `Lightroom API refused the account` | The signed-in account is not a beta user (step 1.4) or has no Lightroom plan. |
| The sign-in page says the redirect URI is invalid | The console's redirect URI must be exactly `https://localhost:<port>/callback`, with the same port as `LR_MCP_PORT`. |
| The sign-in redirect shows another app's page or never loads | Something else listens on the port (`lsof -iTCP:8000 -sTCP:LISTEN`). Use another port as above. |
| `login` times out after 5 minutes | The browser stopped at the certificate warning; proceed past it next time. |
| `Token refresh failed` | Run `lr-mcp login` again. |
| Tools missing in Claude Desktop | Use the absolute path to `lr-mcp`, fully restart the app, and read the log file above. |

## For AI agents

If a user asks you to set this server up:

- **Never ask the user to paste the client secret into the chat, and never print `.env`.** Create the file with empty values (step 2, using the state directory `doctor` prints) and ask the user to fill it in with an editor. To check it without revealing values, `grep -cE '^ADOBE_CLIENT_(ID|SECRET)=.+' <state dir>/.env` must print `2`; `doctor` shows only the first characters of the client ID.
- Install (step 0) and run `lr-mcp doctor` before giving the user the console steps: it checks the callback port first, and the redirect URI they enter must use a free port. Run `doctor` again after every step; a `fix:` that starts with `[human]` is something to hand to the user.
- The user must create the credential, add the beta user, fill in `.env`, and finish the browser sign-in. Give them the exact console steps from step 1, with the port `doctor` reported in the redirect URI and pattern.
- `lr-mcp login` blocks for up to 5 minutes while the user signs in. Run it in the background or ask the user to run it, and warn them about the certificate page.
- Setup is done when `doctor` prints `Catalog found. Setup is complete.` Add the client config (step 5) before or after that. For Claude Desktop, merge into the existing JSON instead of overwriting it, use the absolute path from `which lr-mcp`, pass `LR_MCP_HOME` in `"env"` if it is set, and tell the user to quit and reopen the app.

## Development

```bash
git clone https://github.com/longfin/lr-mcp && cd lr-mcp
uv sync
uv run lr-mcp doctor
uv run pytest
```

To run a checkout from an MCP client, use `uv run --directory /path/to/lr-mcp lr-mcp` as the command. Without installing, `uvx --from git+https://github.com/longfin/lr-mcp lr-mcp <command>` also works.

## License

[MIT](LICENSE)
