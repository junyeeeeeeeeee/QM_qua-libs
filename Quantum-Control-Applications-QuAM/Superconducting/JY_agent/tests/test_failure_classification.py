from __future__ import annotations

import unittest

from jy_agent.failures import classify_failure


class QmServerDetectionError(RuntimeError):
    pass


class QMConnectionError(RuntimeError):
    pass


class FailureClassificationTests(unittest.TestCase):
    def test_server_detection_failure_is_instrument_outage_and_safe_no_connect(self) -> None:
        result = classify_failure(
            QmServerDetectionError("Unable to detect a QOP server")
        )
        self.assertEqual(result.category, "instrument_unreachable")
        self.assertTrue(result.safe_to_release_lock)

    def test_midstream_qm_connection_error_keeps_lock_quarantine(self) -> None:
        result = classify_failure(QMConnectionError("connection reset by QOP"))
        self.assertEqual(result.category, "instrument_unreachable")
        self.assertFalse(result.safe_to_release_lock)

    def test_unrelated_timeout_is_not_misclassified(self) -> None:
        result = classify_failure(TimeoutError("fitting exceeded iteration budget"))
        self.assertIsNone(result.category)
        self.assertFalse(result.safe_to_release_lock)

    def test_wrapped_refused_connection_is_safe_no_connect(self) -> None:
        try:
            try:
                raise ConnectionRefusedError("connection refused")
            except ConnectionRefusedError as cause:
                raise RuntimeError("QOP startup failed") from cause
        except RuntimeError as exc:
            result = classify_failure(exc)
        self.assertEqual(result.category, "instrument_unreachable")
        self.assertTrue(result.safe_to_release_lock)


if __name__ == "__main__":
    unittest.main()
