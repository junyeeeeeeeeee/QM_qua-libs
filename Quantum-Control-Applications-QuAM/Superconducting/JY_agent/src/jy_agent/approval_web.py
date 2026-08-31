from __future__ import annotations

import asyncio
import getpass
import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Collection
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .service import AgentService, ServiceError


MAX_FORM_BYTES = 4096
TAIPEI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Taipei")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

AUTONOMY_EVENTS_SCRIPT = """(() => {
  const eventsUrl = document.body.dataset.autonomyEvents;
  if (!eventsUrl || typeof EventSource === "undefined") return;
  const stream = new EventSource(eventsUrl);
  let pendingRefresh = null;
  stream.addEventListener("refresh", () => {
    if (pendingRefresh !== null) window.clearTimeout(pendingRefresh);
    pendingRefresh = window.setTimeout(() => window.location.reload(), 350);
  });
  window.addEventListener("beforeunload", () => stream.close());
})();
"""

SESSION_DASHBOARD_SCRIPT = """(() => {
  const selector = document.querySelector('[data-experiment-selector]');
  const panels = Array.from(document.querySelectorAll('[data-experiment-panel]'));
  const show = (value) => {
    panels.forEach((panel) => { panel.hidden = panel.dataset.experimentPanel !== value; });
    if (selector) selector.value = value;
    const url = new URL(window.location.href);
    url.searchParams.set('experiment', value);
    window.history.replaceState({}, '', url);
  };
  if (selector && panels.length) {
    selector.addEventListener('change', () => show(selector.value));
    const requested = new URL(window.location.href).searchParams.get('experiment');
    const available = panels.map((panel) => panel.dataset.experimentPanel);
    show(available.includes(requested) ? requested : available[available.length - 1]);
  }
  const eventsUrl = document.body.dataset.sessionEvents;
  if (!eventsUrl || typeof EventSource === 'undefined') return;
  const stream = new EventSource(eventsUrl);
  let pendingRefresh = null;
  stream.addEventListener('refresh', () => {
    if (pendingRefresh !== null) window.clearTimeout(pendingRefresh);
    pendingRefresh = window.setTimeout(() => window.location.reload(), 350);
  });
  window.addEventListener('beforeunload', () => stream.close());
})();
"""

DASHBOARD_EVENT_TYPES = {
    "home": None,
    "approval": {
        "proposal_created",
        "proposal_approved",
        "autonomy_lease_expired",
        "workflow_stopped",
        "full_shutdown_requested",
    },
    "results": {
        "run_started",
        "run_analyzed",
        "run_failed",
        "instrument_error_paused",
        "measurement_paused_for_instrument_error",
        "decision_recorded",
        "autonomy_pause",
        "autonomy_stop",
        "autonomy_emergency_stop",
        "autonomy_halted",
        "autonomy_lease_expired",
        "autonomy_scope_completed",
        "autonomy_resumed",
        "workflow_stopped",
        "full_shutdown_requested",
        "hardware_lock_recovered",
    },
}

LANGUAGE_SCRIPT = """(() => {
  const selector = document.querySelector('[data-language-selector]');
  if (!selector) return;
  const url = new URL(window.location.href);
  const requested = url.searchParams.get('lang');
  let saved = null;
  try { saved = window.localStorage.getItem('jy-dashboard-language'); } catch (_) {}
  if (!requested && (saved === 'en' || saved === 'zh-Hant')) {
    url.searchParams.set('lang', saved);
    window.location.replace(url);
    return;
  }
  selector.addEventListener('change', () => {
    try { window.localStorage.setItem('jy-dashboard-language', selector.value); } catch (_) {}
    const next = new URL(window.location.href);
    next.searchParams.set('lang', selector.value);
    window.location.assign(next);
  });
})();
"""


@dataclass(frozen=True)
class ApprovalPrincipal:
    actor: str
    method: str
    display_name: str


def _requested_language(request: Request) -> str:
    return "en" if request.query_params.get("lang", "").casefold() == "en" else "zh-Hant"


def _ui(language: str, traditional_chinese: str, english: str) -> str:
    return english if language == "en" else traditional_chinese


_ZH_ENUMS = {
    "pending": "待核准",
    "approved": "已核准",
    "rejected": "已拒絕",
    "expired": "已過期",
    "active": "進行中",
    "paused": "已暫停",
    "stopped": "已停止",
    "cancelled_by_shutdown": "因完整關機取消",
    "force_stopped": "已強制停止",
    "halted": "已中止",
    "stopping": "正在安全停止",
    "recovery_required": "需要現場安全復原",
    "recovery_only": "僅限本機復原",
    "waiting_for_run": "等待量測停止",
    "waiting_for_force_exit": "等待強制停止完成",
    "ready": "準備關閉服務",
    "quarantine_ready": "準備隔離並關閉服務",
    "dispatching": "正在關閉服務",
    "running": "執行中",
    "completed": "已完成",
    "failed": "失敗",
    "instrument_unreachable": "儀器無法連線",
    "pass": "通過",
    "needs_review": "需要審閱",
    "manual_review": "人工審閱",
    "conversational": "對話量測",
    "autonomous": "有限自動量測",
    "run": "實驗執行",
    "autonomy_lease": "有限自動授權",
    "state_commit": "狀態寫入",
    "advance": "進到下一節點",
    "repeat": "重複量測",
    "stop": "停止",
}


def _localized_enum(language: str, value: Any) -> str:
    rendered = str(value)
    return _ZH_ENUMS.get(rendered, rendered) if language != "en" else rendered


def _limit_label(language: str, value: Any) -> str:
    if value is None:
        return _ui(language, "無限制", "Unlimited")
    return str(value)


def _language_switch(language: str) -> str:
    zh_selected = " selected" if language != "en" else ""
    en_selected = " selected" if language == "en" else ""
    label = _ui(language, "顯示語言", "Display language")
    return f"""
    <div class="language-switch">
      <label for="language-selector">{escape(label)}</label>
      <select id="language-selector" data-language-selector>
        <option value="zh-Hant"{zh_selected}>繁體中文</option>
        <option value="en"{en_selected}>English</option>
      </select>
    </div>"""


def _json_details(label: str, value: Any, *, card: bool = False) -> str:
    class_name = ' class="card"' if card else ""
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return (
        f"<details{class_name}><summary>{escape(label)}</summary>"
        f"<pre>{escape(payload)}</pre></details>"
    )


def _principal_label(principal: ApprovalPrincipal, language: str) -> str:
    if principal.method == "public_token_browser":
        return _ui(language, "已授權操作人員", "authorized operator")
    if principal.method == "local_browser":
        return _ui(
            language,
            f"{getpass.getuser()}（本機瀏覽器）",
            f"{getpass.getuser()} (local browser)",
        )
    return principal.display_name


def _session_view_redirect(
    session_id: str | None,
    view: str,
    language: str,
) -> RedirectResponse | None:
    if not session_id:
        return None
    query = urlencode({"lang": language})
    return RedirectResponse(
        f"/session/{session_id}/{view}?{query}", status_code=303
    )


async def handle_browser_approval(
    request: Request,
    service: AgentService,
) -> Response:
    """Render or submit one human-only approval review form."""
    language = _requested_language(request)
    try:
        principal = _approval_principal(request, service)
    except ServiceError as exc:
        return _message_page(
            _ui(language, "禁止存取", "Forbidden"),
            str(exc),
            status_code=403,
            language=language,
        )

    proposal_id = str(request.path_params.get("proposal_id", ""))
    try:
        proposal = service.proposal(proposal_id)
    except ServiceError as exc:
        return _message_page(
            _ui(language, "找不到提案", "Proposal not found"),
            str(exc),
            status_code=404,
            language=language,
        )

    if request.method == "GET":
        redirect = _session_view_redirect(
            proposal.get("dashboard_session_id"), "approval", language
        )
        if redirect is not None:
            return redirect
        return _proposal_page(
            service, proposal, principal=principal, language=language
        )

    allowed_origins = {
        *service.approval_allowed_origins,
        _request_origin(request),
    }
    if not _has_trusted_submission_source(request, allowed_origins):
        return _proposal_page(
            service,
            proposal,
            principal=principal,
            error=(
                "Approval refused because the browser submission source was not "
                "trusted."
            ),
            status_code=403,
            language=language,
        )
    if not request.headers.get("content-type", "").lower().startswith(
        "application/x-www-form-urlencoded"
    ):
        return _proposal_page(
            service,
            proposal,
            principal=principal,
            error="Approval refused because the form encoding was invalid.",
            status_code=415,
            language=language,
        )

    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return _proposal_page(
            service,
            proposal,
            principal=principal,
            error="Approval form was too large.",
            status_code=413,
            language=language,
        )
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
        approved = service.approve_from_browser(
            proposal_id,
            _single_field(fields, "confirmation"),
            _single_field(fields, "csrf_token"),
            request.client.host if request.client is not None else "",
            actor=principal.actor,
            approval_method=principal.method,
        )
    except (UnicodeDecodeError, ValueError, ServiceError) as exc:
        return _proposal_page(
            service,
            service.proposal(proposal_id),
            principal=principal,
            error=str(exc),
            status_code=400,
            language=language,
        )
    redirect = _session_view_redirect(
        approved.get("dashboard_session_id"), "approval", language
    )
    if redirect is not None:
        return redirect
    return _proposal_page(
        service,
        approved,
        principal=principal,
        success=_ui(
            language,
            "提案已核准；可以關閉此頁並回到對話。",
            "Proposal approved. You may close this page and return to the conversation.",
        ),
        language=language,
    )


async def handle_browser_approval_asset(
    request: Request,
    service: AgentService,
) -> Response:
    """Serve one proposal-bound raster result image without exposing file paths."""
    language = _requested_language(request)
    try:
        _approval_principal(request, service)
        proposal_id = str(request.path_params.get("proposal_id", ""))
        asset_index = int(str(request.path_params.get("asset_index", "")))
        path = service.proposal_review_asset(proposal_id, asset_index)
    except (ValueError, ServiceError):
        return _message_page(
            _ui(language, "找不到結果圖", "Result image not found"),
            _ui(
                language,
                "此提案無法取得這張結果圖。",
                "This result image is unavailable for the proposal.",
            ),
            status_code=404,
            language=language,
        )
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers=SECURITY_HEADERS,
    )


async def handle_autonomy_control(
    request: Request,
    service: AgentService,
) -> Response:
    """Render and submit authenticated pause/stop/emergency controls."""
    language = _requested_language(request)
    try:
        principal = _approval_principal(request, service)
        lease_id = str(request.path_params.get("lease_id", ""))
        status = service.autonomy_status(lease_id=lease_id)
    except ServiceError as exc:
        return _message_page(
            _ui(language, "無法使用自動量測", "Autonomy unavailable"),
            str(exc),
            status_code=404,
            language=language,
        )
    if request.method == "GET":
        redirect = _session_view_redirect(
            service.dashboard_session_id_for_workflow(str(status["workflow_id"])),
            "results",
            language,
        )
        if redirect is not None:
            return redirect
        return _autonomy_page(service, status, principal, language=language)
    allowed_origins = {
        *service.approval_allowed_origins,
        _request_origin(request),
    }
    if not _has_trusted_submission_source(request, allowed_origins):
        return _autonomy_page(
            service,
            status,
            principal,
            error="Control refused because the browser source was not trusted.",
            status_code=403,
            language=language,
        )
    if not request.headers.get("content-type", "").lower().startswith(
        "application/x-www-form-urlencoded"
    ):
        return _autonomy_page(
            service,
            status,
            principal,
            error="Invalid form encoding.",
            status_code=415,
            language=language,
        )
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return _autonomy_page(
            service,
            status,
            principal,
            error="Control form was too large.",
            status_code=413,
            language=language,
        )
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=3,
        )
        result = service.control_autonomy_from_browser(
            lease_id,
            _single_field(fields, "action"),
            _single_field(fields, "csrf_token"),
            principal.actor,
        )
    except (UnicodeDecodeError, ValueError, ServiceError) as exc:
        return _autonomy_page(
            service,
            service.autonomy_status(lease_id=lease_id),
            principal,
            error=str(exc),
            status_code=400,
            language=language,
        )
    redirect = _session_view_redirect(
        service.dashboard_session_id_for_workflow(str(result["workflow_id"])),
        "results",
        language,
    )
    if redirect is not None:
        return redirect
    return _autonomy_page(
        service,
        result,
        principal,
        success=_ui(
            language,
            f"控制已套用：{result['status']}。",
            f"Control applied: {result['status']}.",
        ),
        language=language,
    )


async def handle_autonomy_control_asset(
    request: Request,
    service: AgentService,
) -> Response:
    """Serve the latest lease-bound result image without exposing disk paths."""
    language = _requested_language(request)
    try:
        _approval_principal(request, service)
        lease_id = str(request.path_params.get("lease_id", ""))
        asset_index = int(str(request.path_params.get("asset_index", "")))
        path = service.autonomy_review_asset(lease_id, asset_index)
    except (ValueError, ServiceError):
        return _message_page(
            _ui(language, "找不到結果圖", "Result image not found"),
            _ui(
                language,
                "此授權無法取得這張即時結果圖。",
                "This live result image is not available for the authorization.",
            ),
            status_code=404,
            language=language,
        )
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers=SECURITY_HEADERS,
    )


async def handle_autonomy_events_script(request: Request) -> Response:
    """Serve the same-origin EventSource client without inline JavaScript."""
    return Response(
        AUTONOMY_EVENTS_SCRIPT,
        media_type="text/javascript",
        headers=SECURITY_HEADERS,
    )


async def handle_language_script(request: Request) -> Response:
    """Serve the same-origin language selector without inline JavaScript."""
    return Response(
        LANGUAGE_SCRIPT,
        media_type="text/javascript",
        headers=SECURITY_HEADERS,
    )


async def handle_autonomy_events(
    request: Request,
    service: AgentService,
) -> Response:
    """Notify the page only for a new analyzed result or a control change."""
    language = _requested_language(request)
    try:
        _approval_principal(request, service)
        lease_id = str(request.path_params.get("lease_id", ""))
        service.autonomy_status(lease_id=lease_id)
        after_id = max(0, int(request.query_params.get("after", "0")))
    except (ValueError, ServiceError) as exc:
        return _message_page(
            _ui(language, "無法使用自動量測", "Autonomy unavailable"),
            str(exc),
            status_code=404,
            language=language,
        )

    async def event_stream():
        cursor = after_id
        yield ": connected\n\n"
        while not await request.is_disconnected():
            update = service.autonomy_ui_events_after(lease_id, cursor)
            cursor = int(update["cursor"])
            events = update["events"]
            if events:
                data = json.dumps(
                    {
                        "cursor": cursor,
                        "events": [item["event_type"] for item in events],
                    },
                    separators=(",", ":"),
                )
                yield f"id: {cursor}\nevent: refresh\ndata: {data}\n\n"
            await asyncio.sleep(0.75)

    stream_headers = dict(SECURITY_HEADERS)
    stream_headers["Cache-Control"] = "no-cache, no-store"
    stream_headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=stream_headers,
    )


async def handle_session_dashboard(
    request: Request,
    service: AgentService,
    *,
    view: str | None = None,
) -> Response:
    """Render one page of the shared measurement Dashboard."""
    language = _requested_language(request)
    resolved_view = view or str(request.path_params.get("view") or "")
    if resolved_view not in DASHBOARD_EVENT_TYPES:
        resolved_view = "home"
    try:
        principal = _approval_principal(request, service)
        session_id = str(request.path_params.get("session_id") or "")
        if not session_id:
            session_id = service.current_dashboard_session_id() or ""
        if not session_id:
            return _dashboard_idle_page(principal, language=language)
        review = service.dashboard_review(session_id)
    except ServiceError as exc:
        return _message_page(
            _ui(language, "無法使用量測工作階段", "Measurement session unavailable"),
            str(exc),
            status_code=404,
            language=language,
        )
    if request.method == "GET" and view is None and request.url.path.startswith(
        "/session/"
    ):
        return _session_view_redirect(session_id, "home", language) or _message_page(
            _ui(language, "無法使用量測工作階段", "Measurement session unavailable"),
            "The session route could not be resolved.",
            status_code=404,
            language=language,
        )
    if request.method == "GET":
        return _session_dashboard_page(
            service, review, principal, view=resolved_view, language=language
        )

    allowed_origins = {*service.approval_allowed_origins, _request_origin(request)}
    if not _has_trusted_submission_source(request, allowed_origins):
        return _session_dashboard_page(
            service,
            review,
            principal,
            view=resolved_view,
            error="Request refused because the browser source was not trusted.",
            status_code=403,
            language=language,
        )
    if not request.headers.get("content-type", "").lower().startswith(
        "application/x-www-form-urlencoded"
    ):
        return _session_dashboard_page(
            service,
            review,
            principal,
            view=resolved_view,
            error="Invalid form encoding.",
            status_code=415,
            language=language,
        )
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return _session_dashboard_page(
            service,
            review,
            principal,
            view=resolved_view,
            error="Form was too large.",
            status_code=413,
            language=language,
        )
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=5,
        )
        operation = _single_field(fields, "operation")
        if view is not None and operation not in {
            "home": {"shutdown"},
            "approval": {"approve"},
            "results": {"control"},
        }[resolved_view]:
            raise ServiceError("This control does not belong to the current Dashboard page.")
        if operation == "approve":
            resolved_view = "approval"
            proposal_id = _single_field(fields, "proposal_id")
            pending = review.get("pending_proposal") or {}
            if proposal_id != str(pending.get("id", "")):
                raise ServiceError("This proposal is no longer the pending session action.")
            service.approve_from_browser(
                proposal_id,
                _single_field(fields, "confirmation"),
                _single_field(fields, "csrf_token"),
                request.client.host if request.client is not None else "",
                actor=principal.actor,
                approval_method=principal.method,
            )
            if str(review.get("session", {}).get("mode")) == "autonomous":
                success = _ui(
                    language,
                    "已核准；AI agent 會在本次有限授權下自動繼續，不需回到對話重複同意。若對話已被平台暫停，請回對話視窗輸入「已核准」後便會開始執行實驗。",
                    "Approved. The AI agent will continue under this bounded authorization without another approval message. If the host paused the conversation, return to it and enter ‘已核准’ to wake the experiment loop.",
                )
            else:
                success = _ui(
                    language,
                    "請回對話視窗輸入「已核准」後便會開始執行實驗。",
                    "Return to the conversation and enter ‘已核准’ to start the experiment.",
                )
        elif operation == "control":
            resolved_view = "results"
            session = review["session"]
            lease_id = str(session.get("autonomy_lease_id") or "")
            if not lease_id:
                raise ServiceError("Manual autonomy controls are unavailable in conversational mode.")
            result = service.control_autonomy_from_browser(
                lease_id,
                _single_field(fields, "action"),
                _single_field(fields, "csrf_token"),
                principal.actor,
            )
            success = _ui(
                language,
                f"控制已套用：{result['status']}。",
                f"Control applied: {result['status']}.",
            )
        elif operation == "shutdown":
            resolved_view = "home"
            result = service.request_full_shutdown_from_browser(
                session_id,
                _single_field(fields, "confirmation"),
                _single_field(fields, "csrf_token"),
                principal.actor,
            )
            if result.get("status") in {"recovery_required", "quarantine_ready"}:
                success = _ui(
                    language,
                    "完整結束已登記；服務會在確認 worker 已離開後關閉，保留的 hardware lock 會被隔離，之後只能從實驗室電腦進行 recovery。",
                    "Full shutdown is queued. After the worker exit is verified, services will close and any retained hardware lock will be quarantined for local recovery on the lab PC.",
                )
            else:
                success = _ui(
                    language,
                    "完整結束已登記；系統會安全等待目前工作停止，接著關閉 workflow、Dashboard 與 tunnel。",
                    "Full shutdown is queued. JY will safely wait for current work, then stop the workflow, Dashboard, and tunnel.",
                )
        else:
            raise ServiceError("Unknown dashboard operation.")
    except (UnicodeDecodeError, ValueError, ServiceError) as exc:
        return _session_dashboard_page(
            service,
            service.dashboard_review(session_id),
            principal,
            view=resolved_view,
            error=str(exc),
            status_code=400,
            language=language,
        )
    return _session_dashboard_page(
        service,
        service.dashboard_review(session_id),
        principal,
        view=resolved_view,
        success=success,
        language=language,
    )


async def handle_session_dashboard_asset(
    request: Request,
    service: AgentService,
) -> Response:
    language = _requested_language(request)
    try:
        _approval_principal(request, service)
        session_id = str(request.path_params.get("session_id", ""))
        asset_index = int(str(request.path_params.get("asset_index", "")))
        path = service.dashboard_review_asset(session_id, asset_index)
    except (ValueError, ServiceError):
        return _message_page(
            _ui(language, "找不到結果圖", "Result image not found"),
            _ui(
                language,
                "此工作階段無法取得這張結果圖。",
                "This session result image is unavailable.",
            ),
            status_code=404,
            language=language,
        )
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers=SECURITY_HEADERS,
    )


async def handle_session_dashboard_script(request: Request) -> Response:
    return Response(
        SESSION_DASHBOARD_SCRIPT,
        media_type="text/javascript",
        headers=SECURITY_HEADERS,
    )


async def handle_session_dashboard_events(
    request: Request,
    service: AgentService,
    *,
    view: str = "home",
) -> Response:
    language = _requested_language(request)
    try:
        _approval_principal(request, service)
        session_id = str(request.path_params.get("session_id", ""))
        service.dashboard_status(session_id)
        after_id = max(0, int(request.query_params.get("after", "0")))
    except (ValueError, ServiceError) as exc:
        return _message_page(
            _ui(language, "無法使用量測工作階段", "Measurement session unavailable"),
            str(exc),
            status_code=404,
            language=language,
        )

    async def event_stream():
        cursor = after_id
        yield ": connected\n\n"
        while not await request.is_disconnected():
            update = service.dashboard_ui_events_after(
                session_id,
                cursor,
                DASHBOARD_EVENT_TYPES.get(view),
            )
            cursor = int(update["cursor"])
            events = update["events"]
            if events:
                data = json.dumps(
                    {
                        "cursor": cursor,
                        "events": [item["event_type"] for item in events],
                    },
                    separators=(",", ":"),
                )
                yield f"id: {cursor}\nevent: refresh\ndata: {data}\n\n"
            await asyncio.sleep(0.75)

    stream_headers = dict(SECURITY_HEADERS)
    stream_headers["Cache-Control"] = "no-cache, no-store"
    stream_headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=stream_headers
    )


def _dashboard_idle_page(
    principal: ApprovalPrincipal,
    *,
    language: str = "zh-Hant",
) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="{escape(language)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script defer src="/assets/language.js"></script>
<title>{_ui(language, 'JY 量測首頁', 'JY measurement home')}</title>
<style>
:root {{ color-scheme:dark; font-family:system-ui,-apple-system,sans-serif; }}
body {{ margin:0; background:#08101d; color:#edf4ff; line-height:1.55; }}
main {{ max-width:760px; margin:48px auto; padding:0 16px; }}
.card {{ background:#121d31; border:1px solid #2b3b5d; border-radius:14px; padding:18px; }}
.muted {{ color:#9fb2d7; }}
.language-switch {{ position:fixed; top:10px; right:12px; padding:8px 10px; background:#121d31; border:1px solid #2b3b5d; border-radius:10px; }}
.language-switch label {{ display:block; color:#9fb2d7; font-size:.75rem; }}
.language-switch select {{ padding:6px; color:white; background:#07101f; border:1px solid #6680b5; border-radius:7px; }}
</style></head><body>{_language_switch(language)}<main>
<h1>{_ui(language, 'JY 量測首頁', 'JY measurement home')}</h1>
<section class="card">
<h2>{_ui(language, '目前沒有開啟中的量測工作階段', 'No measurement session is open')}</h2>
<p>{_ui(language, '服務已可連線；在 AI 對話中進入量測模式後，此入口會顯示新的工作階段。', 'The service is reachable. Enter measurement mode in the AI conversation to make the new session appear here.')}</p>
<p>{_ui(language, '若進入模式因上次殘留的程式、workflow 或設定失敗，請在 repository 的 AI 對話輸入「恢復」；英文輸入 “Recover”。', 'If entry fails because a program, workflow, or lifecycle setting was left over, send “Recover” in the repository AI conversation.')}</p>
<p class="muted">{_ui(language, '審閱者', 'Reviewer')}：{escape(_principal_label(principal, language))}</p>
</section></main></body></html>"""
    return HTMLResponse(html, headers=SECURITY_HEADERS)


def _session_dashboard_page(
    service: AgentService,
    review: dict[str, Any],
    principal: ApprovalPrincipal,
    *,
    view: str = "home",
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
    language: str = "zh-Hant",
) -> HTMLResponse:
    session = review["session"]
    session_id = str(session["id"])
    workflow = session["workflow"]
    authorization = session.get("authorization")
    mode = str(session["mode"])
    pending = review.get("pending_proposal")
    notice = ""
    if error:
        notice = f'<p class="notice error">{escape(error)}</p>'
    elif success:
        notice = f'<p class="notice success">{escape(success)}</p>'
    operational_notice = _instrument_pause_notice(review, language)

    approval_html = ""
    if pending is not None:
        proposal_id = str(pending["id"])
        csrf = service.approval_csrf_token(proposal_id)
        confirmation = f"APPROVE {proposal_id}"
        approval_html = f"""
        <section class="card approval">
          <h2>{_ui(language, '待核准動作', 'Action awaiting approval')}</h2>
          {_proposed_action_section(pending, language)}
          <p>{_ui(language, '確認內容後輸入以下完整文字：', 'Review the action, then enter the complete text below:')}</p>
          <code class="confirmation">{escape(confirmation)}</code>
          <form method="post" action="/session/{escape(session_id)}/approval?lang={escape(language)}">
            <input type="hidden" name="operation" value="approve">
            <input type="hidden" name="proposal_id" value="{escape(proposal_id)}">
            <input type="hidden" name="csrf_token" value="{escape(csrf)}">
            <label for="confirmation">{_ui(language, '核准文字', 'Confirmation')}</label>
            <input id="confirmation" name="confirmation" type="text"
                   autocomplete="off" autocapitalize="off" spellcheck="false" required>
            <button type="submit">{_ui(language, '核准', 'Approve')}</button>
          </form>
        </section>"""
    elif mode == "conversational":
        approval_html = (
            f'<section class="card"><h2>{_ui(language, "核准", "Approval")}</h2>'
            f'<p>{_ui(language, "目前沒有待核准動作。下一個實驗提案建立後，此區會自動更新。", "No action is awaiting approval. This section updates when the next experiment is proposed.")}</p></section>'
        )
    elif authorization is not None:
        approval_html = f"""
        <section class="card approval"><h2>{_ui(language, '有限自動量測授權', 'Bounded automatic measurement authorization')}</h2>
          <p>{_ui(language, '本工作階段採一次核准：授權有效且量測未超出目標、節點、期限與安全參數時，後續實驗不再逐次要求人工同意。', 'This session uses one approval. Later experiments do not require per-run approval while they remain within the authorized targets, nodes, time window, and safety limits.')}</p>
          <dl>
            <dt>{_ui(language, '授權', 'Authorization')}</dt><dd>{escape(str(authorization.get('id')))}</dd>
            <dt>{_ui(language, '狀態', 'Status')}</dt><dd>{escape(_localized_enum(language, authorization.get('status')))}</dd>
            <dt>{_ui(language, '目標', 'Targets')}</dt><dd>{escape(', '.join(authorization.get('targets', [])))}</dd>
            <dt>{_ui(language, '允許的節點', 'Allowed nodes')}</dt><dd>{escape(', '.join(authorization.get('allowed_nodes', [])))}</dd>
            <dt>{_ui(language, '每個節點／qubit 上限', 'Per node/qubit limit')}</dt><dd>{escape(str(authorization.get('max_attempts_per_node_qubit')))}</dd>
            <dt>{_ui(language, '到期時間', 'Expires')}</dt><dd>{escape(_format_taipei_time(authorization.get('expires_at')))}</dd>
          </dl>
          <p class="muted">{_ui(language, '只有超出原授權範圍時，系統才會顯示新的人工核准要求。', 'A new human approval appears only when an action exceeds the existing authorization.')}</p>
        </section>"""

    autonomy_controls = ""
    if authorization is not None and authorization["status"] in {"active", "paused"}:
        lease_id = str(authorization["id"])
        csrf = service.autonomy_csrf_token(lease_id)
        pause_or_resume = (
            f'<button name="action" value="pause" class="pause">{_ui(language, "目前實驗完成後暫停排程", "Pause scheduling after the current run")}</button>'
            if authorization["status"] == "active"
            else f'<button name="action" value="resume" class="resume">{_ui(language, "繼續排程", "Resume scheduling")}</button>'
        )
        autonomy_controls = f"""
        <section class="card"><h2>{_ui(language, '自動授權控制', 'Automation authorization controls')}</h2>
          <p>{_ui(language, '暫停會讓目前實驗完成後不再排新實驗；繼續可恢復同一份未過期授權。', 'Pause lets the current run finish and prevents new scheduling; Resume continues the same unexpired authorization.')}</p>
          <form method="post" action="/session/{escape(session_id)}/results?lang={escape(language)}">
            <input type="hidden" name="operation" value="control">
            <input type="hidden" name="csrf_token" value="{escape(csrf)}">
            {pause_or_resume}
            <button name="action" value="stop" class="stop">{_ui(language, '結束自動授權（保留網站）', 'End automation authorization (keep site)')}</button>
            <p class="muted">{_ui(language, '結束自動授權會撤銷本次 lease，要求目前 worker 安全停止，但保留 workflow、Dashboard 與 server；之後需重新授權才能自動續跑。', 'Ending automation revokes this lease and asks the current worker to stop gracefully, while keeping the workflow, Dashboard, and servers. Automatic work requires a new authorization afterward.')}</p>
            <button name="action" value="emergency_stop" class="emergency">{_ui(language, '緊急停止 worker（保留網站）', 'Emergency-stop worker (keep site)')}</button>
            <p class="warning">{_ui(language, '緊急停止會先要求安全停止，逾時後強制終止 worker；Dashboard 與 server 保持開啟，hardware lock 可能保留並要求現場復原。', 'Emergency stop first requests a graceful stop, then force-terminates the worker after the grace period. The Dashboard and servers stay open; the hardware lock may remain and require local recovery.')}</p>
          </form>
        </section>"""

    shutdown = session.get("shutdown") or {}
    shutdown_status = str(shutdown.get("status") or "")
    shutdown_control = ""
    if (
        session.get("status") != "stopped"
        and shutdown_status != "dispatching"
    ) or shutdown_status == "failed":
        shutdown_csrf = service.dashboard_shutdown_csrf_token(session_id)
        shutdown_confirmation = service.dashboard_shutdown_confirmation(session_id)
        shutdown_notice = ""
        if shutdown_status:
            shutdown_notice = (
                f"<p>{_ui(language, '關閉狀態', 'Shutdown status')}："
                f"{escape(_localized_enum(language, shutdown_status))}</p>"
            )
        if shutdown_status == "failed":
            shutdown_notice += f"""
            <p class="notice error">{_ui(language, '上一次關閉服務的啟動程序失敗；服務仍在運作。請檢查下列錯誤後按一次按鈕重試。', 'The previous service-stop launch failed, so the services are still running. Review the error below and press the button once to retry.')}</p>
            <code class="confirmation">{escape(str(shutdown.get('error') or 'No error detail was recorded.'))}</code>"""
        recovery_help = ""
        if session.get("status") == "recovery_required" or shutdown_status in {
            "recovery_required",
            "quarantine_ready",
        }:
            recovery_help = f"""
            <p class="warning">{_ui(language, 'Hardware lock 會被保留為 recovery quarantine，但不再阻擋網站與 MCP 關機。下次服務只會以本機 recovery-only 模式啟動；請在實驗室電腦使用下列操作頁完成檢查。', 'The hardware lock is retained as a recovery quarantine but no longer blocks Dashboard/MCP shutdown. The next start is local recovery-only; inspect it from the lab PC at the operator page below.')}</p>
            <code class="confirmation">{escape(str(session.get('operator_console_url')))}</code>"""
        shutdown_control = f"""
        <section class="card shutdown"><h2>{_ui(language, '完整結束量測', 'End measurement completely')}</h2>
          {shutdown_notice}
          <p>{_ui(language, '此動作會安全停止或等待 active run，永久結束 workflow，並由本機協調器關閉 MCP service、Dashboard 與 tunnel。即使自動授權已 halted，這個按鈕仍可使用。', 'This safely stops or waits for the active run, permanently ends the workflow, and asks the local coordinator to stop MCP, Dashboard, and the tunnel. It remains available when automation is halted.')}</p>
          {recovery_help}
          <p>{_ui(language, '為避免誤觸，請輸入以下完整文字：', 'To prevent accidental shutdown, enter the complete text below:')}</p>
          <code class="confirmation">{escape(shutdown_confirmation)}</code>
          <form method="post" action="/session/{escape(session_id)}/home?lang={escape(language)}">
            <input type="hidden" name="operation" value="shutdown">
            <input type="hidden" name="csrf_token" value="{escape(shutdown_csrf)}">
            <label for="shutdown-confirmation">{_ui(language, '關機確認文字', 'Shutdown confirmation')}</label>
            <input id="shutdown-confirmation" name="confirmation" type="text"
                   autocomplete="off" autocapitalize="off" spellcheck="false" required>
            <button type="submit" class="shutdown-button">{_ui(language, '結束量測並關閉所有 JY 服務', 'End measurement and stop all JY services')}</button>
          </form>
        </section>"""
    elif shutdown_status:
        shutdown_control = f"""
        <section class="card"><h2>{_ui(language, '完整結束量測', 'Full shutdown')}</h2>
          <p>{_ui(language, '關閉狀態', 'Shutdown status')}：{escape(_localized_enum(language, shutdown_status))}</p>
        </section>"""

    history_html = _session_history_section(
        session_id, review.get("history", []), language
    )
    revision = int(review.get("event_revision", 0))
    events_url = f"/session/{session_id}/{view}/events?after={revision}"
    authorization_status = (
        str(authorization.get("status"))
        if authorization is not None
        else _ui(language, "逐次核准", "per-run approval")
    )
    pending_count = 1 if pending is not None else 0
    result_count = len(review.get("history", []))
    home_path = f"/session/{session_id}/home?lang={language}"
    approval_path = f"/session/{session_id}/approval?lang={language}"
    results_path = f"/session/{session_id}/results?lang={language}"
    nav = f"""
    <nav class="dashboard-nav" aria-label="{_ui(language, 'Dashboard 分頁', 'Dashboard pages')}">
      <a class="{'active' if view == 'home' else ''}" href="{escape(home_path)}">{_ui(language, '首頁', 'Home')}</a>
      <a class="{'active' if view == 'approval' else ''}" href="{escape(approval_path)}">{_ui(language, '核准', 'Approval')} <span class="badge">{pending_count}</span></a>
      <a class="{'active' if view == 'results' else ''}" href="{escape(results_path)}">{_ui(language, '結果與控制', 'Results & controls')} <span class="badge">{result_count}</span></a>
    </nav>"""
    status_html = f"""
    <section class="card"><dl>
    <dt>{_ui(language, '模式', 'Mode')}</dt><dd>{escape(_localized_enum(language, mode))}</dd>
    <dt>{_ui(language, '授權狀態', 'Authorization status')}</dt><dd>{escape(_localized_enum(language, authorization_status))}</dd>
    <dt>{_ui(language, '工作階段狀態', 'Session status')}</dt><dd>{escape(_localized_enum(language, session.get('status')))}</dd>
    <dt>{_ui(language, 'Workflow 狀態', 'Workflow status')}</dt><dd>{escape(_localized_enum(language, workflow.get('status')))}</dd>
    <dt>{_ui(language, '工作流程', 'Workflow')}</dt><dd>{escape(str(workflow['id']))}</dd>
    <dt>{_ui(language, '目前節點', 'Current node')}</dt><dd>{escape(str(workflow.get('current_node')))}</dd>
    <dt>{_ui(language, '目標', 'Targets')}</dt><dd>{escape(', '.join(workflow.get('targets', [])))}</dd>
    <dt>{_ui(language, '工作階段開始時間', 'Session started')}</dt><dd>{escape(_format_taipei_time(session['started_at']))}</dd>
    </dl></section>"""
    device_html = f"""
    <section class="card"><h2>{_ui(language, '連接其他裝置', 'Connect another device')}</h2>
    <p>{_ui(language, '若要讓手機或另一台瀏覽器存取，請在實驗室電腦開啟本機操作頁，為每台裝置建立各自的短效配對連結。不要把長效 token 放入網址。', 'To connect a phone or another browser, open the local operator console on the lab PC and create a separate short-lived pairing link for each device. Never place the long-lived token in a URL.')}</p>
    <code class="confirmation">{escape(str(session.get('operator_console_url')))}</code>
    </section>"""
    continuity_html = f"""
    <section class="card"><h2>{_ui(language, 'AI／網路中斷時怎麼辦', 'If the AI app or network disconnects')}</h2>
    <ol>
      <li>{_ui(language, 'Dashboard 的暫停、停止與完整關機不依賴 AI 對話；手機或電腦仍能開啟頁面時，直接使用頁面控制即可。', 'Dashboard pause, stop, and full-shutdown controls do not depend on the AI conversation. If the page remains reachable, use those controls directly.')}</li>
      <li>{_ui(language, '網路恢復後，可在原對話輸入「結束量測」；英文可輸入 “End measurement”，系統會再次確認並完成安全關機。', 'After connectivity returns, send “End measurement” in the original conversation so JY verifies and completes safe shutdown.')}</li>
      <li>{_ui(language, '若下一次進入量測模式因殘留程式或工作階段失敗，輸入「恢復」；英文可輸入 “Recover”。此命令會安全收尾並關閉可驗證的 JY 程式，不會強制刪除 hardware lock。', 'If the next entry fails because a program or session was left open, send “Recover”. It safely closes verified JY state and programs without force-deleting a hardware lock.')}</li>
    </ol>
    </section>"""
    if view == "approval":
        page_title = _ui(language, "JY 核准", "JY approval")
        page_intro = _ui(language, "此頁只處理待核准動作；新提案出現時才會自動更新。", "This page is only for pending approvals and refreshes when a proposal changes.")
        page_content = approval_html
    elif view == "results":
        page_title = _ui(language, "JY 實驗結果與控制", "JY results and controls")
        page_intro = _ui(language, "所有實驗結果會保留到服務關閉；此頁只在結果、判斷或控制狀態改變時更新。", "All results remain until the service closes. This page refreshes only for results, decisions, or control-state changes.")
        page_content = autonomy_controls + history_html
    else:
        page_title = _ui(language, "JY 量測首頁", "JY measurement home")
        page_intro = _ui(language, "手機與電腦共用此工作階段入口；核准與結果使用下方分頁。AI 對話仍在原本的 App。", "Phones and computers share this session entry. Approval and results use the pages below; AI conversation remains in the original app.")
        page_content = status_html + shutdown_control + continuity_html + device_html
    html = f"""<!doctype html>
<html lang="{escape(language)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script defer src="/assets/language.js"></script>
<script defer src="/assets/session-dashboard.js"></script>
<title>{escape(page_title)}</title>
<style>
:root {{ color-scheme:dark; font-family:system-ui,-apple-system,sans-serif; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#08101d; color:#edf4ff; line-height:1.55; }}
main {{ max-width:1040px; margin:24px auto; padding:0 16px 52px; }}
.language-switch {{ position:fixed; top:10px; right:12px; z-index:10; padding:8px 10px; background:#121d31; border:1px solid #2b3b5d; border-radius:10px; }}
.language-switch label {{ display:block; color:#9fb2d7; font-size:.75rem; margin-bottom:3px; }}
.language-switch select {{ width:auto; padding:6px 28px 6px 8px; }}
.card {{ background:#121d31; border:1px solid #2b3b5d; border-radius:14px; padding:18px; margin:16px 0; }}
dl {{ display:grid; grid-template-columns:160px 1fr; gap:8px 14px; }} dd {{ margin:0; overflow-wrap:anywhere; }}
dt,.muted {{ color:#9fb2d7; }}
pre {{ overflow:auto; white-space:pre-wrap; word-break:break-word; background:#070d18; padding:14px; border-radius:8px; }}
.result-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr)); gap:14px; }}
figure {{ margin:0; padding:10px; background:#070d18; border-radius:10px; }}
img {{ display:block; width:100%; height:auto; border-radius:7px; }}
select,input,button {{ width:100%; padding:12px; border-radius:8px; font:inherit; }}
select,input {{ color:white; background:#07101f; border:1px solid #6680b5; }}
button {{ margin-top:12px; border:0; color:white; background:#3568e8; font-weight:750; cursor:pointer; }}
.pause {{ background:#9a6700; }} .resume {{ background:#16794a; }} .stop {{ background:#b4232d; }}
.emergency {{ background:#ff1738; border:2px solid white; }}
.shutdown-button {{ background:#b4232d; border:2px solid #ffd6dc; }}
.warning {{ padding:12px; border-radius:8px; background:#5b3a12; color:#ffe7b0; }}
.instrument-alert {{ border-color:#f0a93b; background:#3b2b15; }}
.confirmation {{ display:block; padding:12px; background:#070d18; border-radius:8px; user-select:all; overflow-wrap:anywhere; }}
.notice {{ padding:12px; border-radius:8px; }} .error {{ background:#5a1e2a; }} .success {{ background:#14532d; }}
.dashboard-nav {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:18px 0; }}
.dashboard-nav a {{ color:#cfe0ff; background:#121d31; border:1px solid #2b3b5d; border-radius:10px; padding:12px; text-align:center; text-decoration:none; }}
.dashboard-nav a.active {{ background:#234b9d; border-color:#7ba2ff; color:white; }}
.badge {{ display:inline-block; min-width:1.5em; margin-left:4px; padding:0 .4em; border-radius:999px; background:#07101f; }}
[hidden] {{ display:none !important; }}
@media(max-width:600px) {{ dl {{ grid-template-columns:1fr; gap:2px; }} dd {{ margin-bottom:8px; }} .dashboard-nav {{ grid-template-columns:1fr; }} }}
</style></head>
<body data-session-events="{escape(events_url)}">{_language_switch(language)}<main>
<h1>{escape(page_title)}</h1>
<p class="muted">{escape(page_intro)}</p>
<p>{_ui(language, '審閱者', 'Reviewer')}：{escape(_principal_label(principal, language))}</p>{notice}
{nav}
{operational_notice}
{page_content}
</main></body></html>"""
    return HTMLResponse(html, status_code=status_code, headers=SECURITY_HEADERS)


def _instrument_pause_notice(
    review: dict[str, Any], language: str = "zh-Hant"
) -> str:
    for item in reversed(review.get("history", [])):
        analysis = item.get("analysis") or {}
        if analysis.get("failure_category") != "instrument_unreachable":
            continue
        messages = analysis.get("operator_message") or {}
        message = messages.get(language) or messages.get("en") or _ui(
            language,
            "儀器連線失敗，本次實驗與後續排程已暫停。",
            "Instrument connectivity failed; this run and new scheduling are paused.",
        )
        recovery_required = bool(
            analysis.get("hardware_lock_recovery_required")
        )
        quarantine = (
            f"<p>{_ui(language, '本次失聯發生在硬體執行階段，軟體無法證明儀器已完全停止，因此 lock 只保留在本機安全隔離；首頁的完整關機仍可關閉 Dashboard、MCP 與 tunnel。', 'Connectivity was lost during hardware execution, so software cannot prove the instrument is fully idle. The lock remains only in local safety quarantine; Home full shutdown can still close the Dashboard, MCP service, and tunnel.')}</p>"
            if recovery_required
            else ""
        )
        return f"""
        <section class="card instrument-alert">
          <h2>{_ui(language, '儀器錯誤：量測已暫停', 'Instrument error: measurement paused')}</h2>
          <p>{escape(str(message))}</p>
          {quarantine}
          <p>{_ui(language, '連線恢復後，回 AI 對話重新輸入原本的進入量測模式指令；若要結束，輸入「結束量測」／“End measurement”。若重新進入仍失敗，輸入「恢復」／“Recover”。', 'After connectivity returns, re-enter the original JY mode in the AI conversation. To finish, send “End measurement”; if entry still fails, send “Recover”.')}</p>
        </section>"""
    return ""


def _session_history_section(
    session_id: str,
    history: list[dict[str, Any]],
    language: str = "zh-Hant",
) -> str:
    if not history:
        return (
            f'<section class="card"><h2>{_ui(language, "實驗結果", "Experiment results")}</h2>'
            f'<p>{_ui(language, "目前尚無結果；有新實驗或新分析時頁面才會更新。", "No result is available yet. The page updates when a new experiment or analysis is ready.")}</p></section>'
        )
    total = len(history)
    options = "".join(
        f'<option value="{ordinal}">{_ui(language, "實驗", "Experiment")} {ordinal}</option>'
        for ordinal in range(1, total + 1)
    )
    rendered = [
        f'<section class="card"><h2>{_ui(language, "實驗結果", "Experiment results")}</h2>'
        f'<label for="experiment-selector">{_ui(language, "選擇本次啟動後的實驗序號", "Select an experiment from this session")}</label>'
        f'<select id="experiment-selector" data-experiment-selector>{options}</select>'
        '</section>'
    ]
    for ordinal, item in enumerate(history, start=1):
        run = item.get("run") or {}
        analysis = item.get("analysis") or {}
        decision = item.get("decision") or {}
        figures = "".join(
            f'<figure><img src="/session/{escape(session_id)}/assets/{asset_index}" '
            f'alt="{escape(_ui(language, "實驗結果圖", "Experiment result plot"))} {ordinal}-{plot_number}" loading="lazy">'
            f'<figcaption>{_ui(language, "結果圖", "Plot")} {plot_number}</figcaption></figure>'
            for plot_number, asset_index in enumerate(
                item.get("asset_indices", []), start=1
            )
        )
        if not figures:
            figures = f"<p>{_ui(language, '此筆實驗尚無可用的 snapshot 結果圖。', 'No snapshot result plot is available for this experiment.')}</p>"
        evidence = {
            "analysis_status": analysis.get("analysis_status"),
            "failure_reasons": analysis.get("failure_reasons", []),
            "warnings": analysis.get("warnings", []),
            "fit_quality": analysis.get("fit_quality"),
            "dataset_metrics": analysis.get("dataset_metrics"),
        }
        next_action = {
            "next_node": decision.get("next_node"),
            "new_parameters": decision.get("next_parameters", {}),
        }
        rendered.append(f"""
        <section class="card" data-experiment-panel="{ordinal}" hidden>
          <h2>{_ui(language, '實驗', 'Experiment')} {ordinal} / {total} &middot; {escape(str(run.get('node_id')))}</h2>
          <dl>
            <dt>{_ui(language, '執行編號', 'Run')}</dt><dd>{escape(str(run.get('id')))}</dd>
            <dt>{_ui(language, '狀態', 'Status')}</dt><dd>{escape(_localized_enum(language, run.get('status')))}</dd>
            <dt>{_ui(language, '快照', 'Snapshot')}</dt><dd>{escape(str(run.get('snapshot_id')))}</dd>
            <dt>{_ui(language, '耗時', 'Elapsed')}</dt><dd>{escape(_format_seconds(run.get('elapsed_seconds')))}</dd>
          </dl>
          {_json_details(_ui(language, '時間預測', 'Prediction'), item.get('duration_prediction'))}
          {_json_details(_ui(language, '參數', 'Parameters'), run.get('parameters', {}))}
          <div class="result-grid">{figures}</div>
          <h3>{_ui(language, '判斷與理由', 'Decision and reason')}</h3>
          <dl>
            <dt>{_ui(language, '判斷', 'Decision')}</dt><dd>{escape(_localized_enum(language, decision.get('decision') or _ui(language, '待判斷', 'Pending')))}</dd>
            <dt>{_ui(language, '理由', 'Reason')}</dt><dd>{escape(str(decision.get('reason') or _ui(language, '尚未記錄判斷理由。', 'No decision reason has been recorded.')))}</dd>
          </dl>
          {_json_details(_ui(language, '分析', 'Analysis'), evidence)}
          {_json_details(_ui(language, '下一步', 'Next action'), next_action)}
        </section>""")
    return "".join(rendered)


def _autonomy_page(
    service: AgentService,
    status: dict[str, Any],
    principal: ApprovalPrincipal,
    *,
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
    language: str = "zh-Hant",
) -> HTMLResponse:
    lease_id = str(status["id"])
    token = service.autonomy_csrf_token(lease_id)
    notice = ""
    if error:
        notice = f'<p class="notice error">{escape(error)}</p>'
    elif success:
        notice = f'<p class="notice success">{escape(success)}</p>'
    controls = ""
    if status["status"] == "active":
        controls = f"""
        <form method="post" action="/autonomy/{escape(lease_id)}?lang={escape(language)}">
          <input type="hidden" name="csrf_token" value="{escape(token)}">
          <button name="action" value="pause" class="pause">{_ui(language, '本次完成後暫停', 'Pause after current run')}</button>
          <button name="action" value="stop" class="stop">{_ui(language, '安全停止', 'Graceful stop')}</button>
          <button name="action" value="emergency_stop" class="emergency">{_ui(language, '緊急停止', 'Emergency stop')}</button>
        </form>"""
    elif status["status"] == "paused":
        controls = f"""
        <form method="post" action="/autonomy/{escape(lease_id)}?lang={escape(language)}">
          <input type="hidden" name="csrf_token" value="{escape(token)}">
          <button name="action" value="resume" class="resume">{_ui(language, '繼續排程', 'Resume scheduling')}</button>
          <button name="action" value="stop" class="stop">{_ui(language, '撤銷授權', 'Revoke authorization')}</button>
          <button name="action" value="emergency_stop" class="emergency">{_ui(language, '緊急停止', 'Emergency stop')}</button>
        </form>"""
    payload = json.dumps(status, ensure_ascii=False, indent=2, default=str)
    event_revision = service.autonomy_ui_revision(lease_id)
    review_html = _autonomy_history_section(service, lease_id, language)
    events_url = (
        f"/autonomy/{lease_id}/events?after={event_revision}"
        if status["status"] in {"active", "paused"}
        else ""
    )
    event_script = (
        '<script defer src="/assets/autonomy-events.js"></script>'
        if events_url
        else ""
    )
    html = f"""<!doctype html>
<html lang="{escape(language)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script defer src="/assets/language.js"></script>
{event_script}
<title>{_ui(language, 'JY 有限自動量測控制', 'JY bounded automatic measurement control')}</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
body {{ margin:0; background:#08101d; color:#edf4ff; line-height:1.5; }}
main {{ max-width:900px; margin:24px auto; padding:0 16px 48px; }}
.language-switch {{ position:fixed; top:10px; right:12px; z-index:10; padding:8px 10px; background:#121d31; border:1px solid #2b3b5d; border-radius:10px; }}
.language-switch label {{ display:block; color:#9fb2d7; font-size:.75rem; }}
.language-switch select {{ width:auto; padding:6px; color:white; background:#07101f; border:1px solid #6680b5; border-radius:7px; }}
.card {{ background:#121d31; border:1px solid #2b3b5d; border-radius:14px;
padding:18px; margin:16px 0; }}
dl {{ display:grid; grid-template-columns:160px 1fr; gap:8px 14px; }} dd {{ margin:0; }}
button {{ width:100%; margin-top:12px; padding:14px; border:0; border-radius:9px;
font:inherit; font-weight:750; color:white; cursor:pointer; }}
.pause {{ background:#9a6700; }} .resume {{ background:#16794a; }} .stop {{ background:#b4232d; }}
.emergency {{ background:#ff1738; border:2px solid white; }}
pre {{ overflow:auto; white-space:pre-wrap; background:#070d18; padding:14px;
border-radius:8px; }} .notice {{ padding:12px; border-radius:8px; }}
.result-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr)); gap:14px; }}
figure {{ margin:0; padding:10px; background:#070d18; border-radius:10px; }}
img {{ display:block; width:100%; height:auto; border-radius:7px; }}
.error {{ background:#5a1e2a; }} .success {{ background:#14532d; }}
@media(max-width:600px) {{ dl {{ grid-template-columns:1fr; }} }}
</style></head><body data-autonomy-events="{escape(events_url)}">{_language_switch(language)}<main>
<h1>{_ui(language, 'JY 有限自動量測控制', 'JY bounded automatic measurement control')}</h1>
<p>{_ui(language, '審閱者', 'Reviewer')}：{escape(_principal_label(principal, language))}</p>{notice}
<section class="card"><dl>
<dt>{_ui(language, '授權', 'Authorization')}</dt><dd>{escape(lease_id)}</dd>
<dt>{_ui(language, '狀態', 'Status')}</dt><dd>{escape(_localized_enum(language, status['status']))}</dd>
<dt>{_ui(language, '工作流程', 'Workflow')}</dt><dd>{escape(str(status['workflow_id']))}</dd>
<dt>{_ui(language, '目標', 'Targets')}</dt><dd>{escape(', '.join(status['targets']))}</dd>
<dt>{_ui(language, '節點', 'Nodes')}</dt><dd>{escape(', '.join(status['allowed_nodes']))}</dd>
<dt>{_ui(language, '到期時間', 'Expires')}</dt><dd>{escape(_format_taipei_time(status.get('expires_at')))}</dd>
<dt>{_ui(language, '每個節點／qubit 上限', 'Per node/qubit limit')}</dt><dd>{escape(str(status['max_attempts_per_node_qubit']))}</dd>
<dt>{_ui(language, '整體執行次數上限', 'Total run limit')}</dt><dd>{escape(_limit_label(language, status.get('max_total_runs')))}</dd>
<dt>{_ui(language, '已完成目標', 'Resolved targets')}</dt><dd>{escape(json.dumps(status.get('resolved_targets_by_node', {}), ensure_ascii=False))}</dd>
<dt>{_ui(language, '未完成目標', 'Incomplete targets')}</dt><dd>{escape(json.dumps(status.get('incomplete_targets_by_node', {}), ensure_ascii=False))}</dd>
</dl></section>
{review_html}
<section class="card"><h2>{_ui(language, '手動控制', 'Manual controls')}</h2>{controls or f'<p>{_ui(language, "目前沒有可用的控制。", "No active controls.")}</p>'}</section>
<details class="card"><summary>{_ui(language, '完整授權與計數資料', 'Complete authorization and counters')}</summary>
<pre>{escape(payload)}</pre></details>
</main></body></html>"""
    return HTMLResponse(html, status_code=status_code, headers=SECURITY_HEADERS)


def _autonomy_history_section(
    service: AgentService, lease_id: str, language: str = "zh-Hant"
) -> str:
    """Render the append-only run history for one bounded authorization."""
    try:
        review = service.autonomy_review(lease_id)
    except ServiceError as exc:
        return (
            f'<section class="card"><h2>{_ui(language, "量測結果", "Measurement results")}</h2><p>'
            f"{escape(str(exc))}</p></section>"
        )
    history = review.get("history", [])
    if not history:
        return f"""
        <section class="card"><h2>{_ui(language, '本次授權的量測結果', 'Results for this authorization')}</h2>
        <p>{_ui(language, '目前尚無結果。頁面只會在新結果完成分析或手動控制狀態改變時更新。', 'No result is available yet. The page updates only when a new analysis or manual control state is available.')}</p>
        </section>"""

    rendered: list[str] = [
        f'<section class="card"><h2>{_ui(language, "本次授權的完整量測歷史", "Complete measurement history for this authorization")}</h2>'
        f'<p>{_ui(language, "新結果會依時間附加；既有結果不會被下一次量測覆蓋。", "New results are appended over time; later runs do not replace earlier results.")}</p></section>'
    ]
    total = len(history)
    for ordinal, item in enumerate(history, start=1):
        run = item.get("run") or {}
        analysis = item.get("analysis") or {}
        decision = item.get("decision") or {}
        figures = "".join(
            f'<figure><img src="/autonomy/{escape(lease_id)}/assets/{asset_index}" '
            f'alt="{escape(_ui(language, "實驗結果圖", "Experiment result plot"))} {ordinal}-{plot_number}" loading="lazy">'
            f'<figcaption>{_ui(language, "實驗", "Experiment")} {ordinal} &middot; {_ui(language, "結果圖", "Plot")} {plot_number}</figcaption></figure>'
            for plot_number, asset_index in enumerate(
                item.get("asset_indices", []), start=1
            )
        )
        if not figures:
            figures = f"<p>{_ui(language, '此筆量測尚無可用的 snapshot 結果圖。', 'No snapshot result plot is available for this run.')}</p>"
        next_action = {
            "next_node": decision.get("next_node"),
            "new_parameters": decision.get("next_parameters", {}),
        }
        evidence = {
            "analysis_status": analysis.get("analysis_status"),
            "failure_reasons": analysis.get("failure_reasons", []),
            "warnings": analysis.get("warnings", []),
            "fit_quality": analysis.get("fit_quality"),
            "dataset_metrics": analysis.get("dataset_metrics"),
        }
        rendered.append(
            f"""
        <section class="card" id="run-{escape(str(run.get('id')))}">
          <h2>{_ui(language, '實驗', 'Experiment')} {ordinal} / {total} &middot; {escape(str(run.get('node_id')))}</h2>
          <dl>
            <dt>{_ui(language, '執行編號', 'Run')}</dt><dd>{escape(str(run.get('id')))}</dd>
            <dt>{_ui(language, '狀態', 'Status')}</dt><dd>{escape(_localized_enum(language, run.get('status')))}</dd>
            <dt>{_ui(language, '快照', 'Snapshot')}</dt><dd>{escape(str(run.get('snapshot_id')))}</dd>
            <dt>{_ui(language, '耗時', 'Elapsed')}</dt><dd>{escape(_format_seconds(run.get('elapsed_seconds')))}</dd>
          </dl>
          {_json_details(_ui(language, '時間預測', 'Prediction'), item.get('duration_prediction'))}
          {_json_details(_ui(language, '參數', 'Parameters'), run.get('parameters', {}))}
          <div class="result-grid">{figures}</div>
          <h3>{_ui(language, '判斷與理由', 'Decision and reason')}</h3>
          <dl>
            <dt>{_ui(language, '判斷', 'Decision')}</dt><dd>{escape(_localized_enum(language, decision.get('decision') or _ui(language, '待判斷', 'Pending')))}</dd>
            <dt>{_ui(language, '理由', 'Reason')}</dt><dd>{escape(str(decision.get('reason') or _ui(language, '尚未記錄判斷理由。', 'No decision reason has been recorded.')))}</dd>
          </dl>
          {_json_details(_ui(language, '分析', 'Analysis'), evidence)}
          {_json_details(_ui(language, '下一步', 'Next action'), next_action)}
        </section>
        """
        )
    return "".join(rendered)


def _proposal_page(
    service: AgentService,
    proposal: dict[str, Any],
    *,
    principal: ApprovalPrincipal | None = None,
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
    language: str = "zh-Hant",
) -> HTMLResponse:
    proposal_id = str(proposal["id"])
    status = str(proposal["status"])
    payload = json.dumps(
        proposal.get("payload", {}),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    confirmation = f"APPROVE {proposal_id}"
    form = ""
    if status == "pending":
        csrf_token = service.approval_csrf_token(proposal_id)
        form = f"""
        <section class="card approval">
          <h2>{_ui(language, '核准', 'Approval')}</h2>
          <p>{_ui(language, '確認結果、判斷與下一步後，輸入以下完整文字：', 'Review the result, decision, and next action, then enter the complete text below:')}</p>
          <code class="confirmation">{escape(confirmation)}</code>
          <form method="post" action="/approve/{escape(proposal_id)}?lang={escape(language)}">
            <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
            <label for="confirmation">{_ui(language, '核准文字', 'Confirmation')}</label>
            <input id="confirmation" name="confirmation" type="text"
                   autocomplete="off" autocapitalize="off" spellcheck="false" required>
            <button type="submit">{_ui(language, '核准此提案', 'Approve this proposal')}</button>
          </form>
        </section>
        """
    notice = ""
    if error:
        notice = f'<p class="notice error">{escape(error)}</p>'
    elif success:
        notice = f'<p class="notice success">{escape(success)}</p>'

    reviewer = (
        _principal_label(principal, language)
        if principal
        else _ui(language, "本機審閱", "local review")
    )
    review_html = _review_section(service, proposal_id, language)
    proposed_action_html = _proposed_action_section(proposal, language)
    control_url = proposal.get("autonomy_status_url")
    autonomy_link = (
        f'<section class="card"><h2>{_ui(language, "自動量測控制", "Automatic measurement controls")}</h2>'
        f'<p><a href="{escape(str(control_url))}">{_ui(language, "開啟暫停、停止與緊急停止控制", "Open pause, stop, and emergency controls")}</a></p></section>'
        if control_url and status == "approved"
        else ""
    )
    html = f"""<!doctype html>
<html lang="{escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script defer src="/assets/language.js"></script>
  <title>{_ui(language, 'JY 實驗審閱與核准', 'JY experiment review and approval')}</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, -apple-system, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #08101d; color: #edf4ff; line-height: 1.55; }}
    main {{ max-width: 1040px; margin: 28px auto; padding: 0 16px 52px; }}
    .language-switch {{ position:fixed; top:10px; right:12px; z-index:10; padding:8px 10px; background:#121d31; border:1px solid #2b3b5d; border-radius:10px; }}
    .language-switch label {{ display:block; color:#9fb2d7; font-size:.75rem; margin:0 0 3px; }}
    .language-switch select {{ width:auto; padding:6px 28px 6px 8px; color:white; background:#07101f; border:1px solid #6680b5; border-radius:7px; }}
    h1 {{ margin: 0 0 6px; font-size: clamp(1.55rem, 5vw, 2.25rem); }}
    h2 {{ margin-top: 0; }}
    .subtitle, dt {{ color: #9fb2d7; }}
    .card {{ background: #121d31; border: 1px solid #2b3b5d;
      border-radius: 14px; padding: clamp(15px, 4vw, 22px); margin: 16px 0; }}
    .result-grid {{ display: grid; grid-template-columns: repeat(auto-fit,
      minmax(min(100%, 300px), 1fr)); gap: 14px; }}
    figure {{ margin: 0; padding: 10px; background: #070d18; border-radius: 10px; }}
    img {{ display: block; width: 100%; height: auto; border-radius: 7px; }}
    figcaption {{ color: #9fb2d7; margin-top: 8px; overflow-wrap: anywhere; }}
    dl {{ display: grid; grid-template-columns: minmax(110px, 160px) 1fr;
      gap: 8px 16px; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    pre {{ overflow: auto; padding: 14px; background: #070d18; border-radius: 8px;
      white-space: pre-wrap; word-break: break-word; }}
    code {{ overflow-wrap: anywhere; }}
    .confirmation {{ display: block; padding: 12px; background: #070d18;
      border-radius: 8px; user-select: all; }}
    label {{ display: block; margin: 20px 0 6px; }}
    input {{ width: 100%; padding: 12px; font: inherit; border: 1px solid #6680b5;
      border-radius: 8px; background: #07101f; color: white; }}
    button {{ width: 100%; margin-top: 14px; padding: 13px 18px; font: inherit;
      font-weight: 750; border: 0; border-radius: 9px; color: white;
      background: #3568e8; cursor: pointer; }}
    .notice {{ padding: 14px; border-radius: 8px; }}
    a {{ color: #8ab4ff; }}
    .error {{ background: #5a1e2a; }} .success {{ background: #14532d; }}
    .warning {{ color: #ffd479; }} .muted {{ color: #9fb2d7; }}
    details summary {{ cursor: pointer; font-weight: 700; }}
    @media (max-width: 600px) {{
      main {{ margin-top: 18px; }}
      dl {{ grid-template-columns: 1fr; gap: 2px; }}
      dd {{ margin-bottom: 8px; }}
    }}
  </style>
</head>
<body>{_language_switch(language)}
<main>
  <h1>{_ui(language, 'JY 實驗審閱與核准', 'JY experiment review and approval')}</h1>
  <p class="subtitle">{_ui(language, '結果圖、判斷、理由、下一步與核准', 'Result snapshots, decision, reason, next action, and approval')}</p>
  <p class="warning">{_ui(language, '請逐項確認。核准後，下一個動作可能啟動硬體量測或套用 state 變更。', 'Review each item. Approval may allow the next action to start a hardware run or apply a state change.')}</p>
  {notice}
  {review_html}
  {proposed_action_html}
  {autonomy_link}
  <section class="card">
    <h2>{_ui(language, '提案', 'Proposal')}</h2>
    <dl>
      <dt>{_ui(language, '提案編號', 'Proposal')}</dt><dd>{escape(proposal_id)}</dd>
      <dt>{_ui(language, '類型', 'Kind')}</dt><dd>{escape(_localized_enum(language, proposal['kind']))}</dd>
      <dt>{_ui(language, '狀態', 'Status')}</dt><dd>{escape(_localized_enum(language, status))}</dd>
      <dt>{_ui(language, '工作流程', 'Workflow')}</dt><dd>{escape(str(proposal['workflow_id']))}</dd>
      <dt>{_ui(language, '來源用戶端', 'Source client')}</dt><dd>{escape(str(proposal['source_client']))}</dd>
      <dt>{_ui(language, '審閱者', 'Reviewer')}</dt><dd>{escape(reviewer)}</dd>
      <dt>{_ui(language, '建立時間', 'Created')}</dt><dd>{escape(_format_taipei_time(proposal['created_at']))}</dd>
      <dt>{_ui(language, '到期時間', 'Expires')}</dt><dd>{escape(_format_taipei_time(proposal['expires_at']))}</dd>
      <dt>{_ui(language, '使用次數', 'Uses')}</dt><dd>{escape(str(proposal['uses']))} / {escape(str(proposal['max_uses']))}</dd>
    </dl>
  </section>
  <details class="card">
    <summary>{_ui(language, '完整提案資料', 'Complete proposal payload')}</summary>
    <pre>{escape(payload)}</pre>
  </details>
  {form}
</main>
</body>
</html>"""
    return HTMLResponse(html, status_code=status_code, headers=SECURITY_HEADERS)


def _review_section(
    service: AgentService, proposal_id: str, language: str = "zh-Hant"
) -> str:
    try:
        review = service.proposal_review(proposal_id)
    except (AttributeError, ServiceError):
        review = {"available": False, "plots": []}
    if not review.get("available"):
        return f"""
        <section class="card">
          <h2>{_ui(language, '結果', 'Result')}</h2>
          <p class="muted">{_ui(language, '這是第一個提案，或尚無可綁定的前次實驗結果。', 'This is the first proposal, or no previous experiment result can be attached yet.')}</p>
        </section>
        """

    plots = review.get("plots", [])
    figures = "".join(
        f"""
        <figure>
          <img src="/approve/{escape(proposal_id)}/assets/{index}"
               alt="{escape(_ui(language, '結果圖', 'Result plot'))} {index + 1}" loading="lazy">
          <figcaption>{_ui(language, '結果圖', 'Result plot')} {index + 1}</figcaption>
        </figure>
        """
        for index, _ in enumerate(plots)
    )
    if not figures:
        figures = f'<p class="muted">{_ui(language, "此筆量測沒有可用的結果圖。", "No result plot was available for this run.")}</p>'

    run = review.get("run") or {}
    analysis = review.get("analysis") or {}
    decision = review.get("decision") or {}
    decision_value = decision.get("decision") or _ui(
        language, "尚未記錄", "Not recorded"
    )
    reason = decision.get("reason") or _ui(
        language, "尚未記錄判斷理由。", "No decision reason has been recorded."
    )
    next_action = {
        "next_node": decision.get("next_node"),
        "new_parameters": decision.get("next_parameters", {}),
    }
    evidence = {
        "analysis_status": analysis.get("analysis_status"),
        "failure_reasons": analysis.get("failure_reasons", []),
        "warnings": analysis.get("warnings", []),
        "fit_quality": analysis.get("fit_quality"),
        "dataset_metrics": analysis.get("dataset_metrics"),
    }
    return f"""
    <section class="card">
      <h2>{_ui(language, '結果', 'Result')}</h2>
      <dl>
        <dt>{_ui(language, '執行編號', 'Run')}</dt><dd>{escape(str(run.get('id', _ui(language, '未知', 'unknown'))))}</dd>
        <dt>{_ui(language, '節點', 'Node')}</dt><dd>{escape(str(run.get('node_id', _ui(language, '未知', 'unknown'))))}</dd>
        <dt>{_ui(language, '快照', 'Snapshot')}</dt><dd>{escape(str(run.get('snapshot_id', _ui(language, '未知', 'unknown'))))}</dd>
        <dt>{_ui(language, '分析狀態', 'Analysis status')}</dt><dd>{escape(_localized_enum(language, run.get('analysis_status', _ui(language, '未知', 'unknown'))))}</dd>
        <dt>{_ui(language, '耗時', 'Elapsed')}</dt><dd>{escape(_format_seconds(run.get('elapsed_seconds')))}</dd>
      </dl>
      {_json_details(_ui(language, '參數', 'Parameters'), run.get('parameters', {}))}
      <div class="result-grid">{figures}</div>
    </section>
    <section class="card">
      <h2>{_ui(language, '判斷與理由', 'Decision and reason')}</h2>
      <dl>
        <dt>{_ui(language, '判斷', 'Decision')}</dt><dd>{escape(_localized_enum(language, decision_value))}</dd>
        <dt>{_ui(language, '理由', 'Reason')}</dt><dd>{escape(str(reason))}</dd>
      </dl>
      {_json_details(_ui(language, '分析', 'Analysis'), evidence)}
      {_json_details(_ui(language, '下一步', 'Next action'), next_action)}
    </section>
    """


def _proposed_action_section(
    proposal: dict[str, Any], language: str = "zh-Hant"
) -> str:
    payload = proposal.get("payload", {})
    kind = str(proposal.get("kind", "unknown"))
    prediction: Any = None
    parameters: Any = None
    if kind == "run":
        prediction = payload.get("duration_prediction")
        parameters = payload.get("parameters", {})
        action = {
            "kind": "run",
            "node": payload.get("node_id"),
            "reason": payload.get("reason"),
            "warnings": payload.get("warnings", []),
            "prior_decision_experience": payload.get(
                "prior_decision_experience", []
            ),
        }
    elif kind == "autonomy_lease":
        action = {
            "kind": kind,
            "lease_id": payload.get("lease_id"),
            "reason": payload.get("reason"),
            "targets": payload.get("targets", []),
            "allowed_nodes": payload.get("allowed_nodes", []),
            "duration_hours": payload.get("duration_hours"),
            "max_total_runs": payload.get("max_total_runs"),
            "max_attempts_per_node_qubit": payload.get(
                "max_attempts_per_node_qubit"
            ),
            "auto_state_commit_analysis_statuses": payload.get(
                "auto_state_commit_analysis_statuses", []
            ),
            "halt_conditions": payload.get("halt_conditions", []),
            "manual_controls": payload.get("manual_controls", []),
        }
    else:
        action = {
            "kind": kind,
            "reason": payload.get("reason"),
            "run_id": payload.get("run_id"),
            "patch": payload.get("patch", []),
        }
    extra_details = ""
    if kind == "run":
        extra_details = (
            _json_details(_ui(language, "時間預測", "Prediction"), prediction)
            + _json_details(_ui(language, "參數", "Parameters"), parameters)
        )
    return f"""
    <section class="card">
      <h2>{_ui(language, '建議的下一步', 'Proposed next action')}</h2>
      {extra_details}
      {_json_details(_ui(language, '下一步', 'Next action'), action)}
    </section>
    """


def _message_page(
    title: str,
    message: str,
    *,
    status_code: int,
    language: str = "zh-Hant",
) -> HTMLResponse:
    html = (
        f'<!doctype html><html lang="{escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<script defer src="/assets/language.js"></script>'
        "<style>body{font-family:system-ui,sans-serif;padding:2rem}.language-switch{position:fixed;top:10px;right:12px}select{padding:.4rem}</style>"
        f"<title>{escape(title)}</title></head><body>{_language_switch(language)}"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p></body></html>"
    )
    return HTMLResponse(html, status_code=status_code, headers=SECURITY_HEADERS)


def _approval_principal(
    request: Request, service: AgentService
) -> ApprovalPrincipal:
    remote_host = request.client.host if request.client is not None else ""
    settings = service.settings
    if settings.approval_transport == "public":
        if not bool(getattr(request.state, "jy_public_authenticated", False)):
            raise ServiceError("This public dashboard requires its private access link.")
        return ApprovalPrincipal(
            actor="authorized operator via public HTTPS dashboard",
            method="public_token_browser",
            display_name="authorized operator",
        )
    if not _host_in_cidrs(remote_host, settings.approval_trusted_proxy_cidrs):
        raise ServiceError(
            "Approval requests must arrive from the local trusted HTTPS proxy."
        )

    if not _is_loopback(remote_host):
        raise ServiceError("Browser approval is available only through the JY host.")
    return ApprovalPrincipal(
        actor=f"{getpass.getuser()} via local browser ({remote_host})",
        method="local_browser",
        display_name=f"{getpass.getuser()} (local browser)",
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _host_in_cidrs(host: str, cidrs: Collection[str]) -> bool:
    try:
        address = ipaddress.ip_address(host)
        return any(address in ipaddress.ip_network(value) for value in cidrs)
    except ValueError:
        return host.lower() == "localhost" and any(
            ipaddress.ip_address("127.0.0.1") in ipaddress.ip_network(value)
            for value in cidrs
        )


def _request_origin(request: Request) -> str:
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}".rstrip("/")


def _has_trusted_submission_source(
    request: Request,
    expected_origins: str | Collection[str],
) -> bool:
    """Accept configured same-origin forms plus opaque WebView forms with CSRF."""
    origins = (
        {expected_origins.rstrip("/")}
        if isinstance(expected_origins, str)
        else {value.rstrip("/") for value in expected_origins}
    )
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") in origins:
        return True
    if origin not in {None, "null"}:
        return False

    referer = request.headers.get("referer", "")
    if any(referer.startswith(f"{value}/approve/") for value in origins):
        return True
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site in {"same-origin", "none"}:
        return True

    return False


def _single_field(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name)
    if values is None or len(values) != 1:
        raise ValueError(f"Approval form must include one {name} field.")
    return values[0]


def _format_taipei_time(value: Any) -> str:
    """Render an approval timestamp in fixed UTC+08:00 Taipei civil time."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    localized = parsed.astimezone(TAIPEI_TIMEZONE)
    return f"{localized.isoformat(timespec='seconds')} (Asia/Taipei)"


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d} h {minutes:02d} m {remainder:02d} s" if hours else f"{minutes:d} m {remainder:02d} s"
