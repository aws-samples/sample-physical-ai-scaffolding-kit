"""SSH transport to a HyperPod login node, over the cluster's own VPC.

HyperPod nodes are not addressable through the EC2 or SSM control plane from this
account — they carry a SageMaker-owned instance profile and register with SSM in
AWS's account, which is why `ssm:SendCommand` rejects them outright. Their
network interfaces, however, are ordinary ENIs in the cluster's subnet, so a
Lambda attached to that VPC reaches port 22 directly and needs no SSM session.
"""

import base64
import io
import logging
import os
import shlex
from typing import NamedTuple

import boto3
import paramiko

TASK_ROOT = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")

LOGIN_NODE_USER = "ubuntu"
SSH_PORT = 22
# Short enough that a connection and a launch's two commands fit well inside
# the submit Lambda's five-minute timeout.
CONNECT_TIMEOUT_SECONDS = 20
COMMAND_TIMEOUT_SECONDS = 60

logger = logging.getLogger(__name__)


class Result(NamedTuple):
    """The outcome of one remote command."""

    status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.status == 0


class _AcceptUnknownHostKey(paramiko.MissingHostKeyPolicy):
    """Accept any host key without recording it.

    HyperPod replaces nodes, so a pinned key goes stale by design, and the
    function has nowhere durable to record one anyway. The node's name is looked
    up from the SageMaker API on every call, and the route to it never leaves
    the cluster's own subnet.
    """

    def missing_host_key(self, client, hostname, key):
        return


def load_private_key(parameter_name: str) -> paramiko.PKey:
    """Fetch the login-node private key from Parameter Store, in memory only."""
    ssm = boto3.client("ssm")
    pem = ssm.get_parameter(Name=parameter_name, WithDecryption=True)["Parameter"][
        "Value"
    ]

    # ED25519 because that is what the stack creates the key pair as.
    try:
        return paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
    except paramiko.SSHException as error:
        raise RuntimeError(
            f"could not parse an ED25519 private key from {parameter_name}: {error}"
        ) from error


def resolve_login_node(cluster_name: str, login_group_name: str) -> str:
    """Return the private DNS name of a running login node.

    Should be resolved per invocation because HyperPod replaces nodes, which makes a
    configured address go stale.
    """
    sagemaker = boto3.client("sagemaker")
    pages = sagemaker.get_paginator("list_cluster_nodes").paginate(
        ClusterName=cluster_name, InstanceGroupNameContains=login_group_name
    )
    running = sorted(
        (
            node
            for page in pages
            for node in page["ClusterNodeSummaries"]
            if node.get("InstanceGroupName") == login_group_name
            and node.get("InstanceStatus", {}).get("Status") == "Running"
        ),
        key=lambda node: node["InstanceId"],
    )
    if not running:
        raise RuntimeError(
            f"no Running node in instance group '{login_group_name}' of cluster {cluster_name}"
        )
    if len(running) > 1:
        logger.warning(
            "%d running login nodes; using %s. authorized_keys is per-node, so a key "
            "installed on only one of them makes this fail intermittently.",
            len(running),
            running[0]["InstanceId"],
        )

    node = running[0]
    # ListClusterNodes leaves PrivateDnsHostname out on newer clusters.
    details = sagemaker.describe_cluster_node(
        ClusterName=cluster_name, NodeId=node["InstanceId"]
    )["NodeDetails"]
    address = details.get("PrivateDnsHostname")
    if not address:
        raise RuntimeError(
            f"login node {node['InstanceId']} has no PrivateDnsHostname, so there is "
            "no address to connect to"
        )
    return address


def connect(target: str, private_key: paramiko.PKey) -> paramiko.SSHClient:
    """Open an SSH session to the login node. Usable as a context manager."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_AcceptUnknownHostKey())
    logger.info("ssh -> %s", target)
    try:
        client.connect(
            hostname=target,
            port=SSH_PORT,
            username=LOGIN_NODE_USER,
            pkey=private_key,
            timeout=CONNECT_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
    except OSError as error:
        # Distinguished from an auth failure because the causes are different
        # places: this is the function's VPC attachment, the cluster security
        # group's port 22 ingress, or a node that is Running but not yet booted.
        raise RuntimeError(
            f"no route to {target}:{SSH_PORT} ({error}). This function must be "
            "attached to the cluster's VPC, and the cluster security group must "
            "allow port 22 from its security group."
        ) from error
    return client


def run(client: paramiko.SSHClient, command: str) -> Result:
    """Run a command on the login node."""
    _, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
    # Read before recv_exit_status: the remote side can block writing to a full
    # pipe, which would deadlock a wait-then-read ordering.
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return Result(stdout.channel.recv_exit_status(), out, err)


def write_files(
    client: paramiko.SSHClient, directory: str, payloads: dict[str, str]
) -> None:
    """Create `directory` and write each name/content pair into it.

    Base64 rather than a heredoc: each payload is shell-quoted exactly once, so
    nothing in its content can terminate the command early or be expanded by the
    remote shell.
    """
    quoted_dir = shlex.quote(directory)
    commands = [f"mkdir -p {quoted_dir}"]
    for name, content in payloads.items():
        encoded = base64.b64encode(content.encode()).decode()
        target = shlex.quote(f"{directory}/{name}")
        commands.append(f"printf %s {shlex.quote(encoded)} | base64 -d > {target}")

    result = run(client, " && ".join(commands))
    if not result.ok:
        raise RuntimeError(
            f"could not write {directory}: {result.stderr.strip()[:500]}"
        )
