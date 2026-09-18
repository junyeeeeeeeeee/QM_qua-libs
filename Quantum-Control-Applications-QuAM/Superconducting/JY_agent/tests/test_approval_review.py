from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starlette.requests import Request
from starlette.testclient import TestClient

from jy_agent.approval_server import create_approval_app
from jy_agent.approval_web import (
    handle_browser_approval,
    handle_browser_approval_asset,
)
from jy_agent.service import AgentService
from jy_agent.util import utc_now

from test_core import make_settings, sample_state, start_test_workflow


def _request(
    proposal_id: str,
    method: str = "GET",
    *,
    body: bytes = b"",
    host: str = "127.0.0.1:8766",
    origin: str | None = None,
    client_host: str = "127.0.0.1",
    asset_index: int | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("ascii"))]
    if method == "POST":
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    path = f"/approve/{proposal_id}"
    path_params: dict[str, object] = {"proposal_id": proposal_id}
    if asset_index is not None:
        path += f"/assets/{asset_index}"
        path_params["asset_index"] = asset_index
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 50123),
            "server": ("127.0.0.1", 8766),
            "path_params": path_params,
        },
        receive,
    )


class ApprovalReviewTests(unittest.TestCase):
    def test_root_is_the_common_entry_and_legacy_session_url_redirects_home(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", "進入 JY 量測模式", ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            with TestClient(
                create_approval_app(settings), client=("127.0.0.1", 50123)
            ) as client:
                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn("JY 量測首頁", home.text)
                self.assertIn(f"/session/{session_id}/approval", home.text)
                self.assertIn(f"/session/{session_id}/results", home.text)
                legacy = client.get(
                    f"/session/{session_id}", follow_redirects=False
                )
                self.assertEqual(legacy.status_code, 303)
                self.assertEqual(
                    legacy.headers["location"],
                    f"/session/{session_id}/home?lang=zh-Hant",
                )

    def test_stdio_service_reads_dashboard_metadata_created_after_startup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            self.assertFalse(service.remote_approval_enabled)
            self.assertEqual(
                service.browser_approval_origin, "http://127.0.0.1:8766"
            )

            settings.runtime.mkdir(parents=True, exist_ok=True)
            token_path = settings.runtime / "public-dashboard-access.token"
            token_path.write_text("a" * 64, encoding="ascii")
            password_path = settings.runtime / "dashboard-password.txt"
            password_path.write_text("shared-lab-password", encoding="utf-8")
            (settings.runtime / "server-bootstrap.json").write_text(
                json.dumps(
                    {
                        "remote_provider": "public",
                        "approval_bind_host": "127.0.0.1",
                        "approval_port": 8766,
                        "public_base_url": "https://jy-public.example.com",
                        "approval_access_token_path": str(token_path),
                        "dashboard_password_path": str(password_path),
                        "public_tunnel_pid": 123,
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(service.remote_approval_enabled)
            self.assertEqual(
                service.browser_approval_origin,
                "https://jy-public.example.com",
            )
            self.assertEqual(
                service.session_dashboard_url("session-id"),
                "https://jy-public.example.com/",
            )
            self.assertEqual(
                service.session_results_url("session-id"),
                "https://jy-public.example.com/session/session-id/results",
            )

            bootstrap = json.loads(
                (settings.runtime / "server-bootstrap.json").read_text(
                    encoding="utf-8"
                )
            )
            bootstrap["status"] = "stopped"
            (settings.runtime / "server-bootstrap.json").write_text(
                json.dumps(bootstrap), encoding="utf-8"
            )
            self.assertFalse(service.remote_approval_enabled)
            self.assertEqual(
                service.browser_approval_origin, "http://127.0.0.1:8766"
            )

    def test_public_dashboard_requires_password_then_uses_secure_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(
                make_settings(Path(folder), sample_state(0.2, 0.1)),
                approval_transport="public",
                approval_public_base_url="https://jy-public.example.com",
                approval_access_token="a" * 64,
            )
            settings.validate_approval_transport()
            settings.runtime.mkdir(parents=True, exist_ok=True)
            password = "shared-lab-password"
            (settings.runtime / "dashboard-password.txt").write_text(
                password, encoding="utf-8"
            )
            app = create_approval_app(settings)
            with TestClient(app, base_url="https://jy-public.example.com") as client:
                refused = client.get("/healthz", follow_redirects=False)
                self.assertEqual(refused.status_code, 303)
                self.assertTrue(refused.headers["location"].startswith("/login?"))
                forged_host = client.get(
                    "/healthz",
                    headers={"Host": "127.0.0.1:8766"},
                    follow_redirects=False,
                )
                self.assertEqual(forged_host.status_code, 303)
                admitted = client.post(
                    "/login",
                    data={"password": password, "next": "/healthz"},
                    follow_redirects=False,
                )
                self.assertEqual(admitted.status_code, 303)
                self.assertEqual(admitted.headers["location"], "/healthz")
                self.assertTrue(client.cookies.get("jy_dashboard_access"))
                health = client.get("/healthz")
                self.assertEqual(health.status_code, 200)
            with TestClient(app, base_url="https://jy-public.example.com") as invalid:
                rejected = invalid.post(
                    "/login",
                    data={"password": "incorrect-password", "next": "/"},
                    follow_redirects=False,
                )
                self.assertEqual(rejected.status_code, 401)
                self.assertIsNone(invalid.cookies.get("jy_dashboard_access"))

    def test_review_service_exposes_no_mcp_route(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = create_approval_app(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            paths = {route.path for route in app.routes}
            self.assertIn("/", paths)
            self.assertIn("/login", paths)
            self.assertIn("/logout", paths)
            self.assertNotIn("/device-pair", paths)
            self.assertIn("/approve/{proposal_id}", paths)
            self.assertIn(
                "/approve/{proposal_id}/assets/{asset_index:int}", paths
            )
            self.assertIn("/autonomy/{lease_id}/events", paths)
            self.assertIn("/assets/autonomy-events.js", paths)
            self.assertIn("/session/{session_id}", paths)
            self.assertIn("/session/{session_id}/home", paths)
            self.assertIn("/session/{session_id}/approval", paths)
            self.assertIn("/session/{session_id}/results", paths)
            self.assertIn(
                "/session/{session_id}/assets/{asset_index:int}", paths
            )
            self.assertIn("/session/{session_id}/events", paths)
            self.assertIn("/session/{session_id}/home/events", paths)
            self.assertIn("/session/{session_id}/approval/events", paths)
            self.assertIn("/session/{session_id}/results/events", paths)
            self.assertIn("/assets/session-dashboard.js", paths)
            self.assertIn("/assets/language.js", paths)
            self.assertNotIn("/mcp", paths)

    def test_non_loopback_http_approval_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(
                make_settings(Path(folder), sample_state(0.2, 0.1)),
                approval_transport="public",
                approval_host="10.77.0.1",
                approval_public_base_url="http://10.77.0.1:8766",
                approval_access_token="a" * 64,
            )
            with self.assertRaisesRegex(ValueError, "loopback"):
                settings.validate_approval_transport()

    def test_review_page_embeds_proposal_bound_snapshot_asset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            workflow = start_test_workflow(service)
            original = service.request_run(
                workflow["id"],
                "02x",
                {"qubits": ["q1"]},
                "Locate the bare resonator.",
                "unittest",
            )
            snapshot = settings.data_root / "snapshot-1"
            snapshot.mkdir()
            plot = snapshot / "result.png"
            plot.write_bytes(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
                )
            )
            run_id = "review-run"
            service.db.execute(
                """
                INSERT INTO runs(
                    id, workflow_id, proposal_id, node_id, parameters_json,
                    status, analysis_status, analysis_json
                ) VALUES (?, ?, ?, '02x', '{}', 'completed', 'pass', ?)
                """,
                (
                    run_id,
                    workflow["id"],
                    original["id"],
                    json.dumps(
                        {
                            "analysis_status": "pass",
                            "plots": [str(plot)],
                            "warnings": [],
                            "failure_reasons": [],
                        }
                    ),
                ),
            )
            service.db.execute(
                """
                INSERT INTO decisions(
                    id, workflow_id, run_id, decision, reason, next_node,
                    next_parameters_json, state_patch_json, client_id, created_at
                ) VALUES ('decision-1', ?, ?, 'repeat', ?, '02x', '{}', '[]',
                          'unittest', ?)
                """,
                (workflow["id"], run_id, "Confirm with a narrower sweep.", utc_now()),
            )
            followup = service.request_run(
                workflow["id"],
                "02x",
                {"qubits": ["q1"]},
                "Narrow the sweep around the candidate.",
                "unittest",
            )
            response = asyncio.run(
                handle_browser_approval(_request(followup["id"]), service)
            )
            body = response.body.decode("utf-8")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Confirm with a narrower sweep.", body)
            self.assertIn(
                f"/approve/{followup['id']}/assets/0",
                body,
            )
            state_followup = service._create_proposal(
                workflow["id"],
                "state_commit",
                {"patch": [], "reason": "Prepare the next state."},
                "unittest",
                60,
                1,
            )
            state_page = asyncio.run(
                handle_browser_approval(_request(state_followup["id"]), service)
            )
            self.assertIn(
                "Confirm with a narrower sweep.",
                state_page.body.decode("utf-8"),
            )
            asset = asyncio.run(
                handle_browser_approval_asset(
                    _request(followup["id"], asset_index=0), service
                )
            )
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(Path(asset.path).resolve(), plot.resolve())

            outside = Path(folder) / "outside.png"
            outside.write_bytes(b"not an image")
            service.db.execute(
                "UPDATE runs SET analysis_json = ? WHERE id = ?",
                (
                    json.dumps({"analysis_status": "pass", "plots": [str(outside)]}),
                    run_id,
                ),
            )
            refused = asyncio.run(
                handle_browser_approval_asset(
                    _request(followup["id"], asset_index=0), service
                )
            )
            self.assertEqual(refused.status_code, 404)


if __name__ == "__main__":
    unittest.main()
