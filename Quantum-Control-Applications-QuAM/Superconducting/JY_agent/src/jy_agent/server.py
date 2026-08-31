"""Compatibility entry point for the MCP server."""

from .mcp_server import main, mcp

__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
