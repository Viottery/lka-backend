"""Credential-free browser entry; every data request retains its API auth gate."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["distributed client"])
ASSETS = Path(__file__).resolve().parents[2] / "web" / "distributed"
WORKBENCH_PREFIX = "/desktop-pet"
_WORKBENCH_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".webmanifest": "application/manifest+json",
    ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg",
    ".ico": "image/x-icon", ".woff2": "font/woff2", ".atlas": "text/plain; charset=utf-8",
}
_WORKBENCH_CSP = ("default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                  "img-src 'self' data: blob:; media-src 'self' blob:; frame-src 'self' blob:; worker-src 'self'; "
                  "manifest-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")


def _workbench_csp(request: Request) -> str:
    sync = request.app.state.runtime.local_app_config.sync
    if not sync.enabled or sync.role != "replica" or not sync.hub_url:
        return _WORKBENCH_CSP
    # Node-hosted 真理 uses credentialed fetches to exactly the operator's hub.
    # Validate the header interpolation as well as the configuration boundary.
    try:
        hub = urlsplit(sync.hub_url)
        if (hub.scheme != "https" or hub.path not in {"", "/"} or hub.query or hub.fragment
                or not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?", hub.netloc)):
            return _WORKBENCH_CSP
        _ = hub.port  # Reject invalid port ranges before adding a CSP source.
    except ValueError:
        return _WORKBENCH_CSP
    origin = f"https://{hub.netloc}"
    return _WORKBENCH_CSP.replace("connect-src 'self';", f"connect-src 'self' {origin};")


def is_workbench_asset_request(scope) -> bool:
    """Static workbench files are public; middleware lets only these GETs pass."""
    path = scope.get("path", "")
    return (scope.get("type") == "http" and scope.get("method") in {"GET", "HEAD"}
            and (path == WORKBENCH_PREFIX or path.startswith(WORKBENCH_PREFIX + "/")))


def _workbench_root(request: Request):
    configured = request.app.state.runtime.local_app_config.client.workbench_dir
    if not configured:
        return None
    root = Path(configured).expanduser()
    return root.resolve() if root.is_dir() else None


@router.get("/client/info")
def info(request: Request):
    config = request.app.state.runtime.local_app_config
    replica = config.sync.enabled and config.sync.role == "replica"
    return {"role": config.sync.role if config.sync.enabled else "local",
            "node_id": config.connections.node_id or None,
            # Public hub origin so a node page can offer 真理 when the hub is reachable.
            "hub_url": config.sync.hub_url.rstrip("/") if replica and config.sync.hub_url else None,
            "connections_enabled": config.connections.enabled,
            "filesystem_browser_enabled": bool(
                replica and config.connections.node_id and config.connections.filesystem_browser_enabled
            ),
            "execution_enabled": config.remote_execution.enabled,
            "workbench": _workbench_root(request) is not None}


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


@router.get(WORKBENCH_PREFIX, include_in_schema=False)
def workbench_index(request: Request):
    if _workbench_root(request) is None:
        raise HTTPException(404, "workbench not configured")
    return RedirectResponse(WORKBENCH_PREFIX + "/connect.html", status_code=307)


@router.get(WORKBENCH_PREFIX + "/{asset:path}", include_in_schema=False)
def workbench_asset(asset: str, request: Request):
    root = _workbench_root(request)
    if root is None:
        raise HTTPException(404, "workbench not configured")
    parts = asset.split("/")
    if not asset or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise HTTPException(404, "asset not found")
    target = (root / asset).resolve()
    media_type = _WORKBENCH_TYPES.get(target.suffix.lower())
    if media_type is None or not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "asset not found")
    headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer", "Content-Security-Policy": _workbench_csp(request)}
    if target.name == "sw.js":
        headers["Service-Worker-Allowed"] = WORKBENCH_PREFIX + "/"
    return FileResponse(target, media_type=media_type, headers=headers)
