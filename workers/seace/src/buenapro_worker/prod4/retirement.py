"""Operational PROD4 retirement; historical clients and data stay available."""
from typing import NoReturn


PROD4_WORKER_RETIRED_REASON = (
    "PROD4 worker execution is retired; use the PROD2 collector. "
    "Legacy flags cannot reactivate polling, document extraction or matching."
)


class Prod4WorkerRetiredError(RuntimeError):
    """Raised before database, network or model work by retired entry points."""


def reject_prod4_execution() -> NoReturn:
    raise Prod4WorkerRetiredError(PROD4_WORKER_RETIRED_REASON)
