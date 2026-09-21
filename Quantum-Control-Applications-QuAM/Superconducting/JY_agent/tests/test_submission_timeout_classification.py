"""Separating a too-large program from a wedged QOP and from a dead network.

All three surface as a timeout, so the classifier reads where the deadline
expired and the probe asks the instrument whether it is still answering.
"""

from __future__ import annotations

import errno
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jy_agent.failures import classify_failure
from jy_agent.instrument_probe import probe_qop


class QMTimeoutError(RuntimeError):
    """Stand-in for ``qm.exceptions.QMTimeoutError``."""


class _InactiveRpcError(RuntimeError):
    def __init__(self, message: str, status_name: str) -> None:
        super().__init__(message)
        self._status_name = status_name

    def code(self):  # noqa: ANN201 - mirrors the grpc API
        return type("StatusCode", (), {"name": self._status_name})()


def _raise_from_qm_frame(func_name: str, exc: BaseException) -> BaseException:
    """Raise ``exc`` from a frame that looks like it lives in the qm package.

    The classifier keys on the code object's name and file path, so the frame
    is compiled with a ``site-packages/qm/api`` filename.
    """

    source = f"def {func_name}(raise_it):\n    raise_it()\n"
    path = str(Path("site-packages") / "qm" / "api" / "v2" / "qm_api.py")
    namespace: dict[str, object] = {}
    exec(compile(source, path, "exec"), namespace)  # noqa: S102 - test fixture

    def _raise() -> None:
        raise exc

    try:
        namespace[func_name](_raise)
    except BaseException as raised:  # noqa: BLE001 - returned for inspection
        return raised
    raise AssertionError("fixture did not raise")


def _submission_timeout() -> BaseException:
    grpc_error = _InactiveRpcError(
        "status = StatusCode.DEADLINE_EXCEEDED\ndetails = 'Deadline Exceeded'",
        "DEADLINE_EXCEEDED",
    )
    try:
        try:
            raise grpc_error
        except _InactiveRpcError as cause:
            raise QMTimeoutError(
                "A timeout of 100 seconds was reached."
            ) from cause
    except QMTimeoutError as exc:
        return _raise_from_qm_frame("add_to_queue", exc)


class SubmissionTimeoutClassificationTests(unittest.TestCase):
    def test_deadline_while_submitting_is_not_an_outage(self) -> None:
        result = classify_failure(_submission_timeout())
        self.assertEqual(result.category, "program_submission_timeout")
        self.assertFalse(result.safe_to_release_lock)

    def test_deadline_while_connecting_stays_an_outage(self) -> None:
        # Same exception, but no program-submission frame in the traceback.
        try:
            raise QMTimeoutError("A timeout of 100 seconds was reached.")
        except QMTimeoutError as exc:
            result = classify_failure(exc)
        self.assertEqual(result.category, "instrument_unreachable")

    def test_transport_failure_during_submission_stays_an_outage(self) -> None:
        grpc_error = _InactiveRpcError(
            "status = StatusCode.UNAVAILABLE\ndetails = 'failed to connect'",
            "UNAVAILABLE",
        )
        try:
            try:
                raise grpc_error
            except _InactiveRpcError as cause:
                raise QMTimeoutError("A timeout of 100 seconds") from cause
        except QMTimeoutError as exc:
            raised = _raise_from_qm_frame("add_to_queue", exc)
        result = classify_failure(raised)
        self.assertEqual(result.category, "instrument_unreachable")

    def test_unrelated_timeout_is_still_not_classified(self) -> None:
        result = classify_failure(
            TimeoutError("fitting exceeded iteration budget")
        )
        self.assertIsNone(result.category)


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.wiring = Path(self._tmp.name) / "wiring.json"
        self.wiring.write_text(
            '{"network": {"host": "10.0.0.5", "port": 9514, '
            '"cluster_name": "QPX"}}',
            encoding="utf-8",
        )

    def test_answering_qop_reports_healthy(self) -> None:
        with patch("jy_agent.instrument_probe.socket.getaddrinfo"), patch(
            "jy_agent.instrument_probe.socket.create_connection"
        ), patch(
            "jy_agent.instrument_probe._identify"
        ) as identify:
            identify.return_value = type(
                "R", (), {"reachable": True, "stage": "rpc", "cause": "none"}
            )
            result = probe_qop(self.wiring)
        self.assertTrue(result.reachable)

    def test_name_resolution_failure_is_a_network_fault(self) -> None:
        with patch(
            "jy_agent.instrument_probe.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            result = probe_qop(self.wiring)
        self.assertFalse(result.reachable)
        self.assertEqual(result.stage, "dns")
        self.assertEqual(result.cause, "network")

    def test_refused_port_means_the_qop_service_is_down(self) -> None:
        with patch("jy_agent.instrument_probe.socket.getaddrinfo"), patch(
            "jy_agent.instrument_probe.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            result = probe_qop(self.wiring)
        self.assertFalse(result.reachable)
        self.assertEqual(result.stage, "tcp")
        self.assertEqual(result.cause, "instrument")

    def test_unroutable_host_is_a_network_fault(self) -> None:
        unreachable = OSError("no route to host")
        unreachable.errno = errno.EHOSTUNREACH
        with patch("jy_agent.instrument_probe.socket.getaddrinfo"), patch(
            "jy_agent.instrument_probe.socket.create_connection",
            side_effect=unreachable,
        ):
            result = probe_qop(self.wiring)
        self.assertFalse(result.reachable)
        self.assertEqual(result.cause, "network")

    def test_socket_opens_but_qop_never_answers(self) -> None:
        with patch("jy_agent.instrument_probe.socket.getaddrinfo"), patch(
            "jy_agent.instrument_probe.socket.create_connection"
        ), patch(
            "jy_agent.instrument_probe._identify"
        ) as identify:
            from jy_agent.instrument_probe import ProbeResult

            identify.return_value = ProbeResult(
                reachable=False,
                stage="rpc",
                cause="instrument",
                detail="no health check",
            )
            result = probe_qop(self.wiring)
        self.assertFalse(result.reachable)
        self.assertEqual(result.cause, "instrument")

    def test_missing_host_does_not_claim_reachability(self) -> None:
        self.wiring.write_text('{"network": {}}', encoding="utf-8")
        result = probe_qop(self.wiring)
        self.assertFalse(result.reachable)
        self.assertEqual(result.stage, "settings")


class WorkerEscalationTests(unittest.TestCase):
    """The halving ladder stops at one target."""

    def _resolve(self, targets: list[str], reachable: bool):
        from jy_agent.failures import FailureClassification
        from jy_agent.instrument_probe import ProbeResult
        from jy_agent.worker import _resolve_submission_timeout

        probe = ProbeResult(
            reachable=reachable,
            stage="rpc",
            cause="none" if reachable else "instrument",
            detail="",
        )
        with patch("jy_agent.worker.probe_qop", return_value=probe):
            return _resolve_submission_timeout(
                FailureClassification("program_submission_timeout"),
                {"wiring_path": "wiring.json"},
                targets,
            )

    def test_many_targets_keep_running_so_the_group_can_halve(self) -> None:
        classification, probe, confirmed, escalated = self._resolve(
            ["q1", "q2", "q3"], reachable=True
        )
        self.assertEqual(classification.category, "program_submission_timeout")
        self.assertTrue(confirmed)
        self.assertFalse(escalated)
        self.assertTrue(probe.reachable)

    def test_single_target_has_no_room_left_and_escalates(self) -> None:
        _, _, confirmed, escalated = self._resolve(["q1"], reachable=True)
        self.assertTrue(confirmed)
        self.assertTrue(escalated)

    def test_unreachable_probe_reclassifies_as_an_outage(self) -> None:
        classification, _, confirmed, escalated = self._resolve(
            ["q1", "q2"], reachable=False
        )
        self.assertEqual(classification.category, "instrument_unreachable")
        self.assertFalse(confirmed)
        self.assertFalse(escalated)


if __name__ == "__main__":
    unittest.main()
