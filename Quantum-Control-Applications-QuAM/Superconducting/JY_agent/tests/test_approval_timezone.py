from __future__ import annotations

import unittest

from jy_agent.approval_web import _proposal_page


class _FakeService:
    def approval_csrf_token(self, proposal_id: str) -> str:
        return "unused"


class ApprovalTimezoneTests(unittest.TestCase):
    def test_proposal_page_renders_created_and_expires_in_taipei(self) -> None:
        proposal = {
            "id": "proposal-id",
            "kind": "state_commit",
            "status": "expired",
            "workflow_id": "workflow-id",
            "source_client": "unittest",
            "created_at": "2026-08-11T04:16:07+00:00",
            "expires_at": "2026-08-11T05:16:07+00:00",
            "uses": 0,
            "max_uses": 1,
            "payload": {},
        }
        response = _proposal_page(_FakeService(), proposal)
        body = response.body.decode("utf-8")
        self.assertIn(
            "2026-08-11T12:16:07+08:00 (Asia/Taipei)", body
        )
        self.assertIn(
            "2026-08-11T13:16:07+08:00 (Asia/Taipei)", body
        )


if __name__ == "__main__":
    unittest.main()
