"""Submit Lambda for job intake: reaches the cluster's login node over SSH.

Transport lives in `transport.py`. What is here is the entry point, which for
now only probes the login node to prove the transport works end to end.

See docs/en/JOB_INTAKE.md.
"""

import logging
import os
import sys

import paramiko

import transport

CONFIG_KEYS = (
    "CLUSTER_NAME",
    "LOGIN_GROUP_NAME",
    "SSH_KEY_PARAMETER_NAME",
)

PROBE_COMMAND = "hostname; whoami; sinfo -h -o '%P %a %D %t'"

AUTH_FAILED_MESSAGE = (
    "The network path and SSH handshake both succeeded, so the transport is working; "
    "the key pair's public half is not in the login node's authorized_keys. "
    "See docs/en/JOB_INTAKE.md."
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_config() -> dict[str, str]:
    """Read configuration from the environment, raising if anything is unset."""
    missing = [key for key in CONFIG_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"unset environment variables: {', '.join(missing)}")
    return {key: os.environ[key] for key in CONFIG_KEYS}


def _probe(client: paramiko.SSHClient) -> dict:
    """Report the login node's identity and Slurm partitions."""
    result = transport.run(client, PROBE_COMMAND)
    logger.info("probe %s", result)
    return {
        "ok": result.ok,
        "exitStatus": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr[:2000],
        "python": sys.version.split()[0],
    }


def _on_cluster(config: dict[str, str], action) -> dict:
    """Run one action against the login node and return what it produced."""
    private_key = transport.load_private_key(config["SSH_KEY_PARAMETER_NAME"])
    target = transport.resolve_login_node(
        config["CLUSTER_NAME"], config["LOGIN_GROUP_NAME"]
    )
    with transport.connect(target, private_key) as client:
        return {"ok": True, "target": target, **action(client)}


def _run_probe(config: dict[str, str]) -> dict:
    """Open a session and report what the login node says."""
    try:
        return _on_cluster(config, _probe)
    except paramiko.AuthenticationException:
        # Expected until authorized_keys is provisioned, so reported not raised.
        # The node it reached is in the log line transport.connect writes.
        logger.info("reached publickey auth; key not in authorized_keys")
        return {
            "ok": False,
            "reason": "authenticationFailed",
            "message": AUTH_FAILED_MESSAGE,
        }


def lambda_handler(event, context):
    """Probe the login node."""
    config = _load_config()

    if event.get("probeOnly"):
        return _run_probe(config)
    return {"ok": False, "message": "pass probeOnly"}
