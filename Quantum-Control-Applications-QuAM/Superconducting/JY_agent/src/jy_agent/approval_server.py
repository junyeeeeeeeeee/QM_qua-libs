from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .approval_web import (
    handle_autonomy_control,
    handle_autonomy_control_asset,
    handle_autonomy_events,
    handle_autonomy_events_script,
    handle_browser_approval,
    handle_browser_approval_asset,
    handle_language_script,
    handle_session_dashboard,
    handle_session_dashboard_asset,
    handle_session_dashboard_events,
    handle_session_dashboard_script,
)
from .config import Settings
from .dashboard_access import DashboardAccessManager
from .service import AgentService
from .util import atomic_write_json, exclusive_file_lock, parse_iso_datetime


def create_approval_app(settings: Settings | None = None) -> Starlette:
    """Create the review-only web app; it intentionally exposes no MCP route."""
    resolved = settings or Settings.load()
    service = AgentService(resolved)
    dashboard_access = DashboardAccessManager(service.db)

    async def approval(request: Request) -> Response:
        return await handle_browser_approval(request, service)

    async def asset(request: Request) -> Response:
        return await handle_browser_approval_asset(request, service)

    async def autonomy(request: Request) -> Response:
        return await handle_autonomy_control(request, service)

    async def autonomy_asset(request: Request) -> Response:
        return await handle_autonomy_control_asset(request, service)

    async def autonomy_events(request: Request) -> Response:
        return await handle_autonomy_events(request, service)

    async def session_dashboard(request: Request) -> Response:
        return await handle_session_dashboard(request, service)

    async def dashboard_entry(request: Request) -> Response:
        return await handle_session_dashboard(request, service, view="home")

    async def session_home(request: Request) -> Response:
        return await handle_session_dashboard(request, service, view="home")

    async def session_approval(request: Request) -> Response:
        return await handle_session_dashboard(request, service, view="approval")

    async def session_results(request: Request) -> Response:
        return await handle_session_dashboard(request, service, view="results")

    async def session_asset(request: Request) -> Response:
        return await handle_session_dashboard_asset(request, service)

    async def session_events(request: Request) -> Response:
        return await handle_session_dashboard_events(request, service)

    async def session_home_events(request: Request) -> Response:
        return await handle_session_dashboard_events(request, service, view="home")

    async def session_approval_events(request: Request) -> Response:
        return await handle_session_dashboard_events(request, service, view="approval")

    async def session_results_events(request: Request) -> Response:
        return await handle_session_dashboard_events(request, service, view="results")

    async def healthz(request: Request) -> Response:
        status = service.status()
        return JSONResponse(
            {
                "service": "jy-approval",
                "pid": os.getpid(),
                "instance_nonce": os.getenv("JY_SERVICE_INSTANCE_NONCE", ""),
                "remote_approval_enabled": status["server"][
                    "remote_approval_enabled"
                ],
                "approval_endpoint": status["server"]["approval_endpoint"],
                "recovery_only": status["server"]["recovery_only"],
            }
        )

    async def device_pair(request: Request) -> Response:
        if service.current_dashboard_session_id() is None:
            return HTMLResponse(
                "<!doctype html><html><body><h1>Device paired</h1>"
                "<p>No JY measurement session is currently open.</p></body></html>",
                status_code=200,
            )
        return RedirectResponse("/", status_code=303)

    app = Starlette(
        debug=False,
        routes=[
            Route("/", dashboard_entry, methods=["GET"], name="dashboard_entry"),
            Route("/healthz", healthz, methods=["GET"], name="healthz"),
            Route("/device-pair", device_pair, methods=["GET"], name="device_pair"),
            Route(
                "/approve/{proposal_id}",
                approval,
                methods=["GET", "POST"],
                name="browser_approval",
            ),
            Route(
                "/approve/{proposal_id}/assets/{asset_index:int}",
                asset,
                methods=["GET"],
                name="browser_approval_asset",
            ),
            Route(
                "/autonomy/{lease_id}",
                autonomy,
                methods=["GET", "POST"],
                name="autonomy_control",
            ),
            Route(
                "/autonomy/{lease_id}/assets/{asset_index:int}",
                autonomy_asset,
                methods=["GET"],
                name="autonomy_control_asset",
            ),
            Route(
                "/autonomy/{lease_id}/events",
                autonomy_events,
                methods=["GET"],
                name="autonomy_events",
            ),
            Route(
                "/assets/autonomy-events.js",
                handle_autonomy_events_script,
                methods=["GET"],
                name="autonomy_events_script",
            ),
            Route(
                "/session/{session_id}",
                session_dashboard,
                methods=["GET", "POST"],
                name="session_dashboard",
            ),
            Route(
                "/session/{session_id}/home",
                session_home,
                methods=["GET", "POST"],
                name="session_home",
            ),
            Route(
                "/session/{session_id}/approval",
                session_approval,
                methods=["GET", "POST"],
                name="session_approval",
            ),
            Route(
                "/session/{session_id}/results",
                session_results,
                methods=["GET", "POST"],
                name="session_results",
            ),
            Route(
                "/session/{session_id}/assets/{asset_index:int}",
                session_asset,
                methods=["GET"],
                name="session_dashboard_asset",
            ),
            Route(
                "/session/{session_id}/events",
                session_events,
                methods=["GET"],
                name="session_dashboard_events",
            ),
            Route(
                "/session/{session_id}/home/events",
                session_home_events,
                methods=["GET"],
                name="session_home_events",
            ),
            Route(
                "/session/{session_id}/approval/events",
                session_approval_events,
                methods=["GET"],
                name="session_approval_events",
            ),
            Route(
                "/session/{session_id}/results/events",
                session_results_events,
                methods=["GET"],
                name="session_results_events",
            ),
            Route(
                "/assets/language.js",
                handle_language_script,
                methods=["GET"],
                name="language_script",
            ),
            Route(
                "/assets/session-dashboard.js",
                handle_session_dashboard_script,
                methods=["GET"],
                name="session_dashboard_script",
            ),
        ],
    )

    @app.middleware("http")
    async def public_access_token(request: Request, call_next):
        if resolved.approval_transport != "public":
            return await call_next(request)
        expected = resolved.approval_access_token or ""
        health_token = request.headers.get("x-jy-health-token", "")
        if (
            request.url.path == "/healthz"
            and bool(expected)
            and hmac.compare_digest(health_token, expected)
        ):
            return await call_next(request)
        bootstrap_code = request.query_params.get("bootstrap_code", "")
        pairing_code = request.query_params.get("pairing_code", "")
        cookie = request.cookies.get("jy_dashboard_access", "")
        device = dashboard_access.validate_device(cookie)
        issued_device: dict[str, object] | None = None
        if request.method == "GET" and pairing_code:
            issued_device = dashboard_access.consume_pairing(
                pairing_code, actor="browser paired through public HTTPS"
            )
        elif (
            request.method == "GET"
            and bootstrap_code
            and _consume_public_bootstrap_code(resolved.runtime, bootstrap_code)
        ):
            issued_device = dashboard_access.issue_initial_device(
                actor="initial public dashboard bootstrap"
            )
        if issued_device is None and device is None:
            return HTMLResponse(
                "<!doctype html><html><body><h1>Forbidden</h1>"
                "<p>This device is not paired with the JY Dashboard. Create a "
                "short-lived device link from the local operator console on the "
                "lab PC.</p></body></html>",
                status_code=403,
            )
        if issued_device is not None:
            query = [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key not in {"bootstrap_code", "pairing_code"}
            ]
            target = request.url.path
            if query:
                target += "?" + urlencode(query)
            response = RedirectResponse(target, status_code=303)
        else:
            request.state.jy_public_authenticated = True
            response = await call_next(request)
        if issued_device is not None:
            response.set_cookie(
                "jy_dashboard_access",
                str(issued_device["token"]),
                max_age=12 * 60 * 60,
                httponly=True,
                secure=True,
                samesite="strict",
                path="/",
            )
        return response

    return app


def _consume_public_bootstrap_code(
    runtime: os.PathLike[str], supplied: str
) -> bool:
    """Consume one short-lived browser bootstrap code exactly once."""
    runtime_path = os.fspath(runtime)
    code_path = os.path.join(runtime_path, "public-dashboard-bootstrap.json")
    lock_path = os.path.join(runtime_path, "public-dashboard-bootstrap.lock")
    if not os.path.isfile(code_path):
        return False
    try:
        with exclusive_file_lock(Path(lock_path), "public dashboard bootstrap exchange"):
            record = json.loads(Path(code_path).read_text(encoding="utf-8-sig"))
            expires_at = parse_iso_datetime(record.get("expires_at", ""))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            supplied_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
            if (
                bool(record.get("used"))
                or expires_at <= datetime.now(timezone.utc)
                or not hmac.compare_digest(
                    supplied_hash, str(record.get("code_sha256", ""))
                )
            ):
                return False
            record["used"] = True
            record["used_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record.pop("code", None)
            atomic_write_json(Path(code_path), record)
            return True
    except (OSError, ValueError, RuntimeError, TypeError):
        return False


def run_approval_server(
    settings: Settings | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    import uvicorn

    resolved = settings or Settings.load()
    uvicorn.run(
        create_approval_app(resolved),
        host=host or resolved.approval_host,
        port=port or resolved.approval_port,
        log_level="info",
        # Browser bootstrap codes are query parameters for one request.  Never
        # let an HTTP access logger persist them.
        access_log=False,
    )


if __name__ == "__main__":
    run_approval_server()
