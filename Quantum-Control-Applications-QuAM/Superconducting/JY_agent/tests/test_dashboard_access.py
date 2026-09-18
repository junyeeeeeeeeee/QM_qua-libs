from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient

from jy_agent.approval_server import create_approval_app
from jy_agent.dashboard_access import DashboardAccessManager
from jy_agent.db import Database

from test_core import make_settings, sample_state


class DashboardAccessTests(unittest.TestCase):
    def test_pairing_is_single_use_and_only_hashes_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            database = Database(settings.database_path)
            access = DashboardAccessManager(database, instance_nonce="instance-a")

            pairing = access.create_pairing("Lab phone", "local operator")
            stored_pairing = database.one(
                "SELECT * FROM dashboard_pairings WHERE id = ?", (pairing["id"],)
            )
            self.assertNotIn(pairing["code"], json.dumps(stored_pairing))
            self.assertEqual(
                stored_pairing["code_sha256"],
                hashlib.sha256(pairing["code"].encode("utf-8")).hexdigest(),
            )

            device = access.consume_pairing(pairing["code"], actor="phone browser")
            self.assertIsNotNone(device)
            self.assertIsNone(
                access.consume_pairing(pairing["code"], actor="replay attempt")
            )
            stored_device = database.one(
                "SELECT * FROM dashboard_devices WHERE id = ?", (device["id"],)
            )
            self.assertNotIn(device["token"], json.dumps(stored_device))
            self.assertEqual(access.validate_device(device["token"])["id"], device["id"])

            access.revoke_device(device["id"], actor="local operator")
            self.assertIsNone(access.validate_device(device["token"]))

    def test_service_instance_nonce_invalidates_old_device_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            database = Database(settings.database_path)
            old_access = DashboardAccessManager(database, instance_nonce="old-instance")
            device = old_access.issue_initial_device(actor="initial bootstrap")

            new_access = DashboardAccessManager(database, instance_nonce="new-instance")
            self.assertIsNone(new_access.validate_device(device["token"]))
            self.assertEqual(old_access.validate_device(device["token"])["id"], device["id"])

    def test_password_logins_receive_independent_revocable_cookies(self) -> None:
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
            access = DashboardAccessManager(
                Database(settings.database_path), instance_nonce="local-development"
            )

            with TestClient(
                app, base_url="https://jy-public.example.com"
            ) as desktop, TestClient(
                app, base_url="https://jy-public.example.com"
            ) as phone:
                admitted = desktop.post(
                    "/login",
                    data={"password": password, "next": "/healthz"},
                    follow_redirects=False,
                )
                self.assertEqual(admitted.status_code, 303)
                desktop_cookie = desktop.cookies.get("jy_dashboard_access")
                self.assertTrue(desktop_cookie)
                self.assertNotEqual(
                    desktop_cookie,
                    hashlib.sha256(settings.approval_access_token.encode()).hexdigest(),
                )

                paired = phone.post(
                    "/login",
                    data={"password": password, "next": "/healthz"},
                    follow_redirects=False,
                )
                self.assertEqual(paired.status_code, 303)
                self.assertEqual(paired.headers["location"], "/healthz")
                phone_cookie = phone.cookies.get("jy_dashboard_access")
                self.assertTrue(phone_cookie)
                self.assertNotEqual(phone_cookie, desktop_cookie)
                self.assertEqual(desktop.get("/healthz").status_code, 200)
                self.assertEqual(phone.get("/healthz").status_code, 200)

                phone_record = access.validate_device(phone_cookie)
                access.revoke_device(phone_record["id"], actor="local operator")
                refused = phone.get("/healthz", follow_redirects=False)
                self.assertEqual(refused.status_code, 303)
                self.assertEqual(desktop.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
