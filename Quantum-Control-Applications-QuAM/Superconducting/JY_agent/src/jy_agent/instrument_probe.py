"""Live reachability probe used to interpret a program submission timeout.

A deadline that expires while a program is being handed to the QOP looks the
same in the traceback whether the program was simply too large for one
submission or the instrument is wedged.  The traceback cannot separate those
two, so the worker asks the instrument directly, immediately after the
failure.  The probe never touches the experiment: it resolves the host, opens
a socket, and asks the QOP to identify itself.

Each stage narrows the cause:

- name resolution fails, or the host is unroutable  -> network fault
- the port refuses the connection                   -> QOP process is down
- the socket opens but the QOP does not answer      -> QOP is wedged
- the QOP answers                                   -> instrument is healthy,
  so the submission timeout was the program's size

Only the last verdict lets the caller shrink the multiplex group and carry on
without an operator.  Everything else is handed to a human.
"""

from __future__ import annotations

import errno
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_NETWORK_ERRNOS = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.EHOSTDOWN,
}


@dataclass(frozen=True)
class ProbeResult:
    """Verdict of one reachability probe."""

    reachable: bool
    stage: str
    cause: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "stage": self.stage,
            "cause": self.cause,
            "detail": self.detail,
        }


def read_network_settings(wiring_path: Path | str) -> dict[str, Any]:
    """Return the ``network`` block that names the QOP, or an empty mapping."""

    try:
        raw = json.loads(Path(wiring_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    network = raw.get("network") if isinstance(raw, dict) else None
    return network if isinstance(network, dict) else {}


def probe_qop(
    wiring_path: Path | str,
    *,
    tcp_timeout: float = 3.0,
    rpc_timeout: float = 10.0,
) -> ProbeResult:
    """Ask the QOP whether it is alive, without touching the experiment."""

    network = read_network_settings(wiring_path)
    host = str(network.get("host") or "").strip()
    if not host:
        return ProbeResult(
            reachable=False,
            stage="settings",
            cause="unknown",
            detail=(
                "The wiring file does not name a QOP host, so reachability "
                "could not be confirmed."
            ),
        )
    port = _coerce_port(network.get("port"))

    resolved = _resolve(host, port)
    if resolved is not None:
        return resolved

    connected = _connect(host, port, tcp_timeout)
    if connected is not None:
        return connected

    return _identify(host, port, network, rpc_timeout)


def _coerce_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 80
    return port if 0 < port < 65536 else 80


def _resolve(host: str, port: int) -> ProbeResult | None:
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return ProbeResult(
            reachable=False,
            stage="dns",
            cause="network",
            detail=f"Could not resolve QOP host {host}: {exc}",
        )
    except OSError as exc:  # pragma: no cover - defensive
        return ProbeResult(
            reachable=False,
            stage="dns",
            cause="network",
            detail=f"Name resolution for {host} failed: {exc}",
        )
    return None


def _connect(host: str, port: int, timeout: float) -> ProbeResult | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except ConnectionRefusedError as exc:
        return ProbeResult(
            reachable=False,
            stage="tcp",
            cause="instrument",
            detail=(
                f"{host}:{port} refused the connection, so the QOP service is "
                f"not running: {exc}"
            ),
        )
    except socket.timeout:
        return ProbeResult(
            reachable=False,
            stage="tcp",
            cause="network",
            detail=(
                f"No response from {host}:{port} within {timeout:g} s at the "
                "socket level."
            ),
        )
    except OSError as exc:
        cause = "network" if exc.errno in _NETWORK_ERRNOS else "instrument"
        return ProbeResult(
            reachable=False,
            stage="tcp",
            cause=cause,
            detail=f"Could not open a socket to {host}:{port}: {exc}",
        )


def _identify(
    host: str, port: int, network: dict[str, Any], timeout: float
) -> ProbeResult:
    """Ask the QOP to identify itself over its own client."""

    try:
        from qm import QuantumMachinesManager
    except Exception as exc:  # pragma: no cover - qm always present in situ
        return ProbeResult(
            reachable=True,
            stage="tcp",
            cause="none",
            detail=(
                f"{host}:{port} accepted a socket; the QM client was "
                f"unavailable for a full health check ({exc})."
            ),
        )

    settings: dict[str, Any] = {"host": host, "timeout": timeout}
    if network.get("port") is not None:
        settings["port"] = port
    cluster_name = network.get("cluster_name")
    if cluster_name:
        settings["cluster_name"] = cluster_name
    try:
        QuantumMachinesManager(**settings)
    except Exception as exc:
        return ProbeResult(
            reachable=False,
            stage="rpc",
            cause="instrument",
            detail=(
                f"{host}:{port} accepted a socket but the QOP did not complete "
                f"a health check within {timeout:g} s: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    return ProbeResult(
        reachable=True,
        stage="rpc",
        cause="none",
        detail=(
            f"The QOP at {host}:{port} answered a health check after the "
            "submission timeout, so the instrument and network are healthy."
        ),
    )
