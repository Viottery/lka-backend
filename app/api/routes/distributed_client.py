"""Credential-free browser entry; every data request retains its API auth gate."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(tags=["distributed client"])
ASSETS = Path(__file__).resolve().parents[2] / "web" / "distributed"


@router.get("/client/info")
def info(request: Request):
    config = request.app.state.runtime.local_app_config
    return {"role": config.sync.role if config.sync.enabled else "local",
            "node_id": config.connections.node_id or None,
            "connections_enabled": config.connections.enabled,
            "execution_enabled": config.remote_execution.enabled}


@router.get("/client")
@router.get("/client/{asset}")
def client(asset: str = "client.html"):
    if asset not in {"client.html", "client.js", "client.css"}:
        raise HTTPException(404, "asset not found")
    return FileResponse(ASSETS / asset, headers={
        "Content-Security-Policy": "default-src 'self'; connect-src 'self'; script-src 'self'; "
                                   "style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
