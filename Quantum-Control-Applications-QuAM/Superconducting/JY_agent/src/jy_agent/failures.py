from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class FailureClassification:
    category: str | None
    safe_to_release_lock: bool = False


_INSTRUMENT_CONNECTION_TYPES = {
    "qmconnectionerror",
    "qmtimeouterror",
    "qmhealthcheckerror",
    "qmfailedtogetquantummachineerror",
    "qmfailedtoclosequantummachineerror",
    "qmfailedtocloseallquantummachineserror",
    "qmserverdetectionerror",
    "octaveconnectionerror",
    "octaveloopbackerror",
    "connecterror",
    "connecttimeout",
    "newconnectionerror",
    "nameresolutionerror",
}

_NO_CONNECTION_ESTABLISHED_TYPES = {
    "connectionrefusederror",
    "gaierror",
    "qmserverdetectionerror",
    "connecterror",
    "connecttimeout",
    "newconnectionerror",
    "nameresolutionerror",
}

_CONNECTION_MESSAGE_MARKERS = (
    "failed to connect",
    "unable to connect",
    "could not connect",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "host is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "server detection",
    "failed to get quantum machine",
    "qop server",
    "opx connection",
    "octave connection",
    "grpc_status:14",
    "statuscode.unavailable",
    "deadline exceeded",
)

_QOP_COMPILE_FAILURE_TYPES = {
    "qopresponseerror",
    "failedtoexecutejobexception",
}

_QOP_COMPILE_MESSAGE_MARKERS = (
    "compilation failed",
    "internal error. please report it to qm",
)


def classify_failure(exc: BaseException) -> FailureClassification:
    """Classify recognizable instrument and compile-time QOP failures.

    Generic timeouts and arbitrary ``OSError`` values are intentionally not
    enough on their own: a fitting or file timeout must remain a normal worker
    failure.  ``safe_to_release_lock`` is true when either:

    - a transport connection was never established, or
    - QOP rejected the program at compile/queue time with an Internal error
      (job never owned the hardware, so retaining the lock is unnecessary).
    """

    chain = tuple(_exception_chain(exc))
    type_names = {type(item).__name__.casefold() for item in chain}
    messages = "\n".join(str(item).casefold() for item in chain)

    if _looks_like_qop_compile_failure(type_names, messages):
        return FailureClassification(
            "qop_compile_failure",
            safe_to_release_lock=True,
        )

    specific_type = bool(type_names & _INSTRUMENT_CONNECTION_TYPES)
    builtin_connection = bool(
        type_names
        & {
            "connectionerror",
            "connectionrefusederror",
            "connectionreseterror",
            "connectionabortederror",
            "gaierror",
        }
    )
    marked_message = any(marker in messages for marker in _CONNECTION_MESSAGE_MARKERS)
    if not (specific_type or builtin_connection or marked_message):
        return FailureClassification(None)

    safe_to_release = bool(type_names & _NO_CONNECTION_ESTABLISHED_TYPES)
    return FailureClassification(
        "instrument_unreachable",
        safe_to_release_lock=safe_to_release,
    )


def _looks_like_qop_compile_failure(
    type_names: set[str], messages: str
) -> bool:
    has_compile_type = bool(type_names & _QOP_COMPILE_FAILURE_TYPES)
    has_compile_message = any(
        marker in messages for marker in _QOP_COMPILE_MESSAGE_MARKERS
    )
    # Require both a QM execute/compile exception type and the known markers so
    # unrelated "internal error" strings elsewhere do not release the lock.
    return has_compile_type and has_compile_message


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
