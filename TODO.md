# Backlog

- **Optional stateless streamable-HTTP mode** (config flag) so server restarts
  never invalidate client sessions. Today the server runs stateful (required so
  GET /mcp SSE works for mcp-remote — see build_asgi_app); a restart makes
  clients' `Mcp-Session-Id` stale and they must re-initialize (the 404
  `Session not found` they get is spec-compliant and correct). A
  `stateless_http` add-on option could trade the SSE keep-alive stream for
  restart-proof sessions where the client supports it.
