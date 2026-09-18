from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jy_agent.failures import classify_failure
from jy_agent.worker import _stop_active_node_with_evidence


class QmServerDetectionError(RuntimeError):
    pass


class QMConnectionError(RuntimeError):
    pass


class FailureClassificationTests(unittest.TestCase):
    def test_midstream_cleanup_requires_a_recorded_active_node(self) -> None:
        stopped = Mock()
        qualibrate = SimpleNamespace(
            QualibrationNode=SimpleNamespace(
                active_node=SimpleNamespace(stop=stopped)
            )
        )
        with patch.dict("sys.modules", {"qualibrate": qualibrate}):
            verified, error = _stop_active_node_with_evidence()
        self.assertTrue(verified)
        self.assertIsNone(error)
        stopped.assert_called_once_with()

        qualibrate.QualibrationNode.active_node = None
        with patch.dict("sys.modules", {"qualibrate": qualibrate}):
            verified, error = _stop_active_node_with_evidence()
        self.assertFalse(verified)
        self.assertIn("unavailable", error)

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

    def test_qop_compile_internal_error_is_safe_to_release_lock(self) -> None:
        class QopResponseError(RuntimeError):
            pass

        class FailedToExecuteJobException(RuntimeError):
            pass

        try:
            try:
                raise QopResponseError(
                    "Error from QOP, details:\nCompilation failed."
                )
            except QopResponseError as cause:
                raise FailedToExecuteJobException(
                    "Failed to execute program. See the following errors:\n"
                    "Internal error. Please report it to QM (ts=1788520966629)"
                ) from cause
        except FailedToExecuteJobException as exc:
            result = classify_failure(exc)
        self.assertEqual(result.category, "qop_compile_failure")
        self.assertTrue(result.safe_to_release_lock)

    def test_generic_internal_error_string_alone_does_not_release_lock(self) -> None:
        result = classify_failure(
            RuntimeError("Internal error. Please report it to QM")
        )
        self.assertIsNone(result.category)
        self.assertFalse(result.safe_to_release_lock)


if __name__ == "__main__":
    unittest.main()
