"""Dependencies passed to every state handler of the VAMS poller."""

import os
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pask_vams_poller.config import PollerConfig

if TYPE_CHECKING:
    from pask_vams_poller.sfn import SfnNotifier
    from pask_vams_poller.slurm import SlurmClient
    from pask_vams_poller.store import ExecutionStore


@dataclass
class PollerContext:
    """Clients, settings, and the clock shared by ``process_<state>(item, ctx)`` handlers."""

    config: PollerConfig
    store: "ExecutionStore"
    sfn: "SfnNotifier"
    slurm: "SlurmClient"
    s3: Any
    sqs: Any
    training_env: dict[str, str]
    owner_id: str
    clock: Callable[[], float] = field(default=time.time)


def build_owner_id(environ: Mapping[str, str]) -> str:
    """Build the lease owner id of this poller process.

    Args:
        environ: Process environment. systemd sets ``INVOCATION_ID`` for each start.

    Returns:
        ``<hostname>:<INVOCATION_ID>``, or ``<hostname>:<pid>`` outside systemd.
    """
    invocation_id = environ.get("INVOCATION_ID") or str(os.getpid())
    return f"{socket.gethostname()}:{invocation_id}"
