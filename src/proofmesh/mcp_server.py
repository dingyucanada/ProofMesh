from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Serve the role MCP gateways and the internal Action Gateway over Streamable HTTP."""

    uvicorn.run(
        "proofmesh.api:app",
        host=os.getenv("PROOFMESH_HOST", "127.0.0.1"),
        port=int(os.getenv("PROOFMESH_PORT", "8000")),
        log_level=os.getenv("PROOFMESH_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
