"""Toolsets. One module per domain; each exposes `register(registrar)`.

`tbmcp.server.build_server` imports them in `config.ALL_TOOLSETS` order so
`tools/list` is deterministic, which lets clients and prompt caches hit.
"""
