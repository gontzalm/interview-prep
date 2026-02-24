from .mcp_a2a import mcp

app = mcp.http_app(path="/", stateless_http=True)
