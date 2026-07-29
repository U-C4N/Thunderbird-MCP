"""thunderbird-mcp — an MCP server that drives a local Thunderbird.

Public surface is deliberately small; the CLI is the intended entry point.
"""

from .config import ALL_TOOLSETS, DEFAULT_TOOLSETS, Settings

__all__ = ["ALL_TOOLSETS", "DEFAULT_TOOLSETS", "Settings"]
