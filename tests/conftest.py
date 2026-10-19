import os
import tempfile

# Must be set before lr_mcp.client is imported: it reads the state directory and .env at import time.
os.environ["LR_MCP_HOME"] = tempfile.mkdtemp(prefix="lr-mcp-test-")
os.environ["ADOBE_CLIENT_ID"] = "test-client-id"
os.environ["ADOBE_CLIENT_SECRET"] = "test-client-secret"
