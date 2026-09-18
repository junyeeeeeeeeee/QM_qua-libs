from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .util import parse_iso_datetime, utc_now


class DashboardAccessError(RuntimeError):
    pass


class DashboardAccessManager:
    """Issue auditable, revocable browser sessions for the Dashboard."""

    def __init__(
        self,
        database: Database,
        *,
        instance_nonce: str | None = None,
    ) -> None:
        self.db = database
        self.instance_nonce = str(
            instance_nonce
            if instance_nonce is not None
            else os.getenv("JY_SERVICE_INSTANCE_NONCE", "local-development")
        ).strip()
        if not self.instance_nonce:
            raise DashboardAccessError("A JY service instance nonce is required")

    def create_pairing(
        self,
        label: str,
        actor: str,
        *,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        normalized_label = self._label(label)
        if not actor.strip():
            raise DashboardAccessError("Pairing creator identity is required")
        if not 1 <= int(ttl_minutes) <= 30:
            raise DashboardAccessError("Pairing lifetime must be 1-30 minutes")
        pairing_id = uuid.uuid4().hex
        code = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=int(ttl_minutes))
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE dashboard_pairings SET status = 'expired' "
                "WHERE instance_nonce = ? AND status = 'pending' AND expires_at <= ?",
                (self.instance_nonce, created.isoformat(timespec="seconds")),
            )
            connection.execute(
                "INSERT INTO dashboard_pairings(id, instance_nonce, code_sha256, "
                "label, status, created_at, expires_at, created_by) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    pairing_id,
                    self.instance_nonce,
                    self._sha256(code),
                    normalized_label,
                    created.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                    actor.strip(),
                ),
            )
            self.db.event(
                "dashboard_pairing_created",
                actor.strip(),
                {
                    "pairing_id": pairing_id,
                    "label": normalized_label,
                    "expires_at": expires.isoformat(timespec="seconds"),
                },
                connection=connection,
            )
        return {
            "id": pairing_id,
            "code": code,
            "label": normalized_label,
            "status": "pending",
            "expires_at": expires.isoformat(timespec="seconds"),
        }

    def consume_pairing(self, code: str, *, actor: str) -> dict[str, Any] | None:
        candidate = str(code).strip()
        if len(candidate) < 32:
            return None
        now = datetime.now(timezone.utc)
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM dashboard_pairings WHERE instance_nonce = ? "
                "AND code_sha256 = ?",
                (self.instance_nonce, self._sha256(candidate)),
            ).fetchone()
            if row is None:
                return None
            pairing = dict(row)
            try:
                expires = parse_iso_datetime(pairing["expires_at"])
            except (TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if pairing["status"] != "pending" or expires <= now:
                if pairing["status"] == "pending":
                    connection.execute(
                        "UPDATE dashboard_pairings SET status = 'expired' WHERE id = ?",
                        (pairing["id"],),
                    )
                return None
            changed = connection.execute(
                "UPDATE dashboard_pairings SET status = 'used', used_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now.isoformat(timespec="seconds"), pairing["id"]),
            ).rowcount
            if changed != 1:
                return None
            device = self._issue_device_with_connection(
                connection,
                label=str(pairing["label"]),
                actor=actor,
                now=now,
            )
            self.db.event(
                "dashboard_pairing_consumed",
                actor,
                {
                    "pairing_id": pairing["id"],
                    "device_id": device["id"],
                    "label": pairing["label"],
                },
                connection=connection,
            )
            return device

    def issue_initial_device(self, *, actor: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.db.transaction(immediate=True) as connection:
            device = self._issue_device_with_connection(
                connection,
                label="Initial dashboard device",
                actor=actor,
                now=now,
            )
            self.db.event(
                "dashboard_initial_device_paired",
                actor,
                {"device_id": device["id"]},
                connection=connection,
            )
            return device

    def issue_password_session(self, *, actor: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.db.transaction(immediate=True) as connection:
            device = self._issue_device_with_connection(
                connection,
                label="Password-authenticated browser",
                actor=actor,
                now=now,
            )
            self.db.event(
                "dashboard_password_login",
                actor,
                {"device_id": device["id"]},
                connection=connection,
            )
            return device

    def validate_device(self, token: str) -> dict[str, Any] | None:
        candidate = str(token).strip()
        if len(candidate) < 32:
            return None
        now = datetime.now(timezone.utc)
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM dashboard_devices WHERE instance_nonce = ? "
                "AND token_sha256 = ? AND status = 'active'",
                (self.instance_nonce, self._sha256(candidate)),
            ).fetchone()
            if row is None:
                return None
            device = dict(row)
            try:
                expires = parse_iso_datetime(device["expires_at"])
            except (TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                connection.execute(
                    "UPDATE dashboard_devices SET status = 'expired' WHERE id = ?",
                    (device["id"],),
                )
                return None
            connection.execute(
                "UPDATE dashboard_devices SET last_seen_at = ? WHERE id = ?",
                (now.isoformat(timespec="seconds"), device["id"]),
            )
            device.pop("token_sha256", None)
            return device

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT id, label, status, created_at, expires_at, last_seen_at, "
            "revoked_at, created_by FROM dashboard_devices "
            "WHERE instance_nonce = ? ORDER BY created_at DESC",
            (self.instance_nonce,),
        )
        return rows

    def list_pairings(self) -> list[dict[str, Any]]:
        now = utc_now()
        self.db.execute(
            "UPDATE dashboard_pairings SET status = 'expired' "
            "WHERE instance_nonce = ? AND status = 'pending' AND expires_at <= ?",
            (self.instance_nonce, now),
        )
        return self.db.all(
            "SELECT id, label, status, created_at, expires_at, used_at, created_by "
            "FROM dashboard_pairings WHERE instance_nonce = ? "
            "ORDER BY created_at DESC LIMIT 20",
            (self.instance_nonce,),
        )

    def revoke_device(self, device_id: str, *, actor: str) -> dict[str, Any]:
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, label, status FROM dashboard_devices "
                "WHERE id = ? AND instance_nonce = ?",
                (device_id, self.instance_nonce),
            ).fetchone()
            if row is None:
                raise DashboardAccessError("Unknown dashboard device")
            changed = connection.execute(
                "UPDATE dashboard_devices SET status = 'revoked', revoked_at = ? "
                "WHERE id = ? AND status = 'active'",
                (now, device_id),
            ).rowcount
            self.db.event(
                "dashboard_device_revoked",
                actor,
                {"device_id": device_id, "label": row["label"]},
                connection=connection,
            )
        return {"id": device_id, "status": "revoked", "changed": changed == 1}

    def revoke_all(self, *, actor: str) -> int:
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE dashboard_devices SET status = 'revoked', revoked_at = ? "
                "WHERE instance_nonce = ? AND status = 'active'",
                (now, self.instance_nonce),
            ).rowcount
            connection.execute(
                "UPDATE dashboard_pairings SET status = 'revoked' "
                "WHERE instance_nonce = ? AND status = 'pending'",
                (self.instance_nonce,),
            )
            self.db.event(
                "dashboard_access_revoked_all",
                actor,
                {"device_count": changed},
                connection=connection,
            )
        return int(changed)

    def _issue_device_with_connection(
        self,
        connection: Any,
        *,
        label: str,
        actor: str,
        now: datetime,
    ) -> dict[str, Any]:
        device_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(hours=12)
        connection.execute(
            "INSERT INTO dashboard_devices(id, instance_nonce, token_sha256, "
            "label, status, created_at, expires_at, last_seen_at, created_by) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (
                device_id,
                self.instance_nonce,
                self._sha256(token),
                self._label(label),
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                actor,
            ),
        )
        return {
            "id": device_id,
            "token": token,
            "label": self._label(label),
            "status": "active",
            "expires_at": expires.isoformat(timespec="seconds"),
        }

    @staticmethod
    def _label(value: str) -> str:
        label = " ".join(str(value).strip().split())
        if not label or len(label) > 80:
            raise DashboardAccessError("Device label must contain 1-80 characters")
        return label

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
