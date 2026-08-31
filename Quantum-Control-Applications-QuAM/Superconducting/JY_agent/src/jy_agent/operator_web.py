from __future__ import annotations

import getpass
import hmac
import ipaddress
import json
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .dashboard_access import DashboardAccessError, DashboardAccessManager
from .recovery import HardwareLockRecovery, HardwareLockRecoveryError
from .service import AgentService, ServiceError


OPERATOR_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
MAX_OPERATOR_FORM_BYTES = 8192


async def handle_operator_console(
    request: Request,
    service: AgentService,
    access: DashboardAccessManager,
) -> Response:
    client_host = request.client.host if request.client else ""
    request_host = request.url.hostname or ""
    if not _is_loopback(client_host) or not _is_loopback(request_host):
        return HTMLResponse(
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Forbidden</title>"
            "</head><body><h1>Forbidden</h1><p>The JY operator console is "
            "available only through a loopback URL on the lab computer.</p>"
            "</body></html>",
            status_code=403,
            headers=OPERATOR_SECURITY_HEADERS,
        )

    pairing_link: str | None = None
    success: str | None = None
    error: str | None = None
    if request.method == "POST":
        try:
            if not request.headers.get("content-type", "").lower().startswith(
                "application/x-www-form-urlencoded"
            ):
                raise ServiceError("Invalid operator form encoding")
            body = await request.body()
            if len(body) > MAX_OPERATOR_FORM_BYTES:
                raise ServiceError("Operator form was too large")
            fields = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
            supplied_csrf = _single(fields, "csrf_token")
            if not hmac.compare_digest(supplied_csrf, service.operator_csrf_token()):
                raise ServiceError("The operator-console token was invalid")
            operation = _single(fields, "operation")
            actor = f"{getpass.getuser()} via local operator console"
            if operation == "create_pairing":
                if service.approval_transport != "public":
                    raise ServiceError("Public Dashboard access is not currently enabled")
                pairing = access.create_pairing(
                    _single(fields, "label"), actor, ttl_minutes=10
                )
                pairing_link = (
                    f"{service.browser_approval_origin}/device-pair?"
                    + urlencode({"pairing_code": pairing["code"]})
                )
                success = (
                    "A one-time device link was created. Open it on the intended "
                    "phone/browser within ten minutes."
                )
            elif operation == "revoke_device":
                result = access.revoke_device(_single(fields, "device_id"), actor=actor)
                success = f"Device access is {result['status']}."
            elif operation == "recover_lock":
                if _single(fields, "hardware_attestation") != "confirmed":
                    raise HardwareLockRecoveryError(
                        "Confirm that the connected hardware has no active job/output."
                    )
                run_id = _single(fields, "run_id")
                recovery = HardwareLockRecovery(service.settings, service.db)
                result = recovery.recover(
                    run_id,
                    actor=actor,
                    operator_confirmation=_single(fields, "confirmation"),
                )
                service.reconcile_full_shutdown_request()
                success = (
                    f"Hardware lock for run {result['run_id']} was safely archived. "
                    "Any queued full shutdown will now continue automatically."
                )
            else:
                raise ServiceError("Unknown operator-console operation")
        except (
            UnicodeDecodeError,
            ValueError,
            ServiceError,
            DashboardAccessError,
            HardwareLockRecoveryError,
        ) as exc:
            error = str(exc)
    return _page(
        service,
        access,
        pairing_link=pairing_link,
        success=success,
        error=error,
        status_code=400 if error else 200,
    )


def _page(
    service: AgentService,
    access: DashboardAccessManager,
    *,
    pairing_link: str | None = None,
    success: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    csrf = service.operator_csrf_token()
    notice = ""
    if error:
        notice = f'<p class="notice error">{escape(error)}</p>'
    elif success:
        notice = f'<p class="notice success">{escape(success)}</p>'

    pairing_result = ""
    if pairing_link:
        pairing_result = f"""
        <section class="card highlight"><h2>New device pairing link</h2>
          <p>This link is single-use and expires in ten minutes. Send it only to the intended device.</p>
          <code>{escape(pairing_link)}</code>
        </section>"""

    devices = access.list_devices()
    device_rows = "".join(
        f"""<tr><td>{escape(str(item['label']))}</td><td>{escape(str(item['status']))}</td>
        <td>{escape(str(item.get('last_seen_at') or 'never'))}</td><td>{escape(str(item['expires_at']))}</td>
        <td>{_revoke_form(item, csrf) if item['status'] == 'active' else ''}</td></tr>"""
        for item in devices
    ) or '<tr><td colspan="5">No device has been paired for this service instance.</td></tr>'

    pairings = access.list_pairings()
    pairing_rows = "".join(
        f"<tr><td>{escape(str(item['label']))}</td><td>{escape(str(item['status']))}</td>"
        f"<td>{escape(str(item['expires_at']))}</td></tr>"
        for item in pairings
    ) or '<tr><td colspan="3">No recent pairing codes.</td></tr>'

    recovery_html = _recovery_section(service, csrf)
    status = service.status()
    workflow = status.get("workflow") or {}
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JY local operator console</title><style>
:root{{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}}*{{box-sizing:border-box}}
body{{margin:0;background:#08101d;color:#edf4ff;line-height:1.5}}main{{max-width:1100px;margin:24px auto;padding:0 16px 48px}}
.card{{background:#121d31;border:1px solid #2b3b5d;border-radius:14px;padding:18px;margin:16px 0}}
.highlight{{border-color:#68a1ff}}.warning{{background:#5b3a12;color:#ffe7b0;padding:12px;border-radius:8px}}
.notice{{padding:12px;border-radius:8px}}.error{{background:#5a1e2a}}.success{{background:#14532d}}
input,button{{width:100%;padding:11px;border-radius:8px;font:inherit}}input{{background:#07101f;color:white;border:1px solid #6680b5}}
button{{margin-top:10px;border:0;background:#3568e8;color:white;font-weight:750;cursor:pointer}}button.danger{{background:#b4232d}}
code{{display:block;padding:12px;background:#070d18;border-radius:8px;overflow-wrap:anywhere;user-select:all}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #2b3b5d;text-align:left;vertical-align:top}}
.scroll{{overflow:auto}}label.check{{display:flex;gap:10px;align-items:flex-start}}label.check input{{width:auto;margin-top:5px}}
</style></head><body><main>
<h1>JY local operator console</h1>
<p class="warning">Local lab computer only. This control surface is served on the MCP loopback port and is not routed through the public Dashboard tunnel.</p>
{notice}
<section class="card"><h2>Current state</h2><p>Workflow: {escape(str(workflow.get('id') or 'none'))}</p>
<p>Workflow status: {escape(str(workflow.get('status') or 'none'))}; active worker: {escape(str(bool(status.get('active_run'))))}; hardware lock: {escape(str(bool(status.get('hardware_lock'))))}</p></section>
{pairing_result}
<section class="card"><h2>Pair another phone or browser</h2>
<p>Create one separate, short-lived link per device. The long-lived service secret is never placed in the URL.</p>
<form method="post" action="/operator"><input type="hidden" name="operation" value="create_pairing">
<input type="hidden" name="csrf_token" value="{escape(csrf)}"><label>Device label
<input name="label" maxlength="80" placeholder="e.g. Richard's phone" required></label>
<button type="submit">Create 10-minute pairing link</button></form></section>
<section class="card"><h2>Paired devices</h2><div class="scroll"><table><thead><tr><th>Label</th><th>Status</th><th>Last seen</th><th>Expires</th><th>Action</th></tr></thead><tbody>{device_rows}</tbody></table></div></section>
<section class="card"><h2>Recent pairing codes</h2><div class="scroll"><table><thead><tr><th>Label</th><th>Status</th><th>Expires</th></tr></thead><tbody>{pairing_rows}</tbody></table></div></section>
{recovery_html}
</main></body></html>"""
    return HTMLResponse(html, status_code=status_code, headers=OPERATOR_SECURITY_HEADERS)


def _revoke_form(item: dict[str, Any], csrf: str) -> str:
    return f"""<form method="post" action="/operator">
    <input type="hidden" name="operation" value="revoke_device">
    <input type="hidden" name="csrf_token" value="{escape(csrf)}">
    <input type="hidden" name="device_id" value="{escape(str(item['id']))}">
    <button type="submit" class="danger">Revoke</button></form>"""


def _recovery_section(service: AgentService, csrf: str) -> str:
    if not service.settings.lock_path.exists():
        return '<section class="card"><h2>Hardware-lock recovery</h2><p>No retained hardware lock is present.</p></section>'
    try:
        lock = json.loads(service.settings.lock_path.read_text(encoding="utf-8-sig"))
        run_id = str(lock.get("run_id") or "")
        inspection = HardwareLockRecovery(service.settings, service.db).inspect(run_id)
    except Exception as exc:
        return (
            '<section class="card"><h2>Hardware-lock recovery</h2>'
            f'<p class="notice error">Inspection failed: {escape(str(exc))}</p></section>'
        )
    checks = inspection.get("checks", {})
    failure_list = "".join(
        f"<li>{escape(str(item))}</li>" for item in inspection.get("failures", [])
    )
    if not inspection.get("ready"):
        return f"""<section class="card"><h2>Hardware-lock recovery</h2>
        <p class="notice error">Recovery is blocked. The lock remains untouched.</p>
        <ul>{failure_list}</ul><code>{escape(json.dumps(checks, ensure_ascii=False, indent=2))}</code></section>"""
    mismatch = ""
    if checks.get("configured_state_differs_from_run"):
        mismatch = (
            '<p class="warning">The currently configured state path differs from the state file used by this run. '
            'The verified run-specific active/recovery/database hashes still match; this difference will remain in the receipt.</p>'
        )
    confirmation = f"RECOVER HARDWARE LOCK {run_id}"
    return f"""<section class="card"><h2>Hardware-lock recovery</h2>
    <p>All software checks pass: lock/run/request identity, no live worker, no active database run, matching run state and recovery hashes, and no restore error.</p>{mismatch}
    <code>{escape(json.dumps(checks, ensure_ascii=False, indent=2))}</code>
    <p class="warning">Before continuing, physically inspect the connected QOP/OPX and instruments. Do not recover while any job or output is active.</p>
    <form method="post" action="/operator"><input type="hidden" name="operation" value="recover_lock">
    <input type="hidden" name="csrf_token" value="{escape(csrf)}"><input type="hidden" name="run_id" value="{escape(run_id)}">
    <label class="check"><input type="checkbox" name="hardware_attestation" value="confirmed" required>
    <span>I inspected the connected hardware and confirm there is no active job/output.</span></label>
    <p>Type exactly:</p><code>{escape(confirmation)}</code>
    <input name="confirmation" autocomplete="off" spellcheck="false" required>
    <button type="submit" class="danger">Safely archive the retained lock</button></form></section>"""


def _single(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name)
    if values is None or len(values) != 1:
        raise ValueError(f"Operator form must include one {name} field")
    return values[0]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"
