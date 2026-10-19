# lr-mcp

An MCP server for **Adobe Lightroom (cloud)** — the Lightroom app that syncs to Creative Cloud, not Lightroom Classic. It talks to the [Lightroom Partner API](https://developer.adobe.com/lightroom/lightroom-api-docs/) (`https://lr.adobe.io`) with your own Adobe credentials.

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

### 1. Create an Adobe integration

Each user needs their own credential. Until Adobe reviews an integration it is limited to 25 beta users, so a shared client ID is not practical.

1. Open the [Adobe Developer Console](https://developer.adobe.com/console) and create a project.
2. **Add API** → **Lightroom Services** → **OAuth Web App**.
3. Set the redirect URI to `https://localhost:8000/callback` and the redirect URI pattern to `https://localhost:8000/.*`. Use another port if 8000 is taken, and set `LR_MCP_PORT` to match.
4. Under **Lightroom Services → Manage beta users**, add the Adobe account you will sign in with.
5. Copy the **Client ID** and **Client Secret** from the credential page.

The signed-in account needs an active Lightroom subscription or trial.

### 2. Configure

Settings come from environment variables, or from a `.env` file in the state directory (`~/.config/lr-mcp/` by default):

| Variable | Required | Description |
|---|---|---|
| `ADOBE_CLIENT_ID` | yes | Client ID (API key) |
| `ADOBE_CLIENT_SECRET` | yes | Client secret |
| `LR_MCP_PORT` | no | Local callback port, default `8000` |
| `LR_MCP_HOME` | no | State directory, default `$XDG_CONFIG_HOME/lr-mcp` or `~/.config/lr-mcp` |

```bash
mkdir -p ~/.config/lr-mcp
cat > ~/.config/lr-mcp/.env <<'EOF'
ADOBE_CLIENT_ID=your-client-id
ADOBE_CLIENT_SECRET=your-client-secret
EOF
chmod 600 ~/.config/lr-mcp/.env
```

### 3. Sign in

```bash
uvx lr-mcp login
```

A browser window opens. The local callback server uses a self-signed certificate (created with `openssl`), so the browser shows a certificate warning for `localhost`; proceed past it. Tokens are stored in the state directory with mode 600 and refreshed automatically.

You can also skip this step and ask the assistant to run `lr_login`.

### 4. Add to your MCP client

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "lightroom": {
      "command": "uvx",
      "args": ["lr-mcp"]
    }
  }
}
```

Claude Desktop does not always see your shell's `PATH`; if `uvx` is not found, use its absolute path (`which uvx`). You can also pass `ADOBE_CLIENT_ID` and `ADOBE_CLIENT_SECRET` in an `"env"` block instead of the `.env` file.

Claude Code:

```bash
claude mcp add lightroom -s user -- uvx lr-mcp
```

## Development

```bash
git clone <this repo> && cd lr-mcp
uv sync
uv run lr-mcp login
```

To run from a checkout, replace `uvx lr-mcp` with `uv run --directory /path/to/lr-mcp lr-mcp`.
