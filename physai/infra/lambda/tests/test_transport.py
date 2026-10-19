"""Tests for the SSH transport to the login node.

Covers what the transport decides rather than what paramiko does: which node it
connects to, how it reports an unreachable one, and how it writes files without
the remote shell interpreting their contents.
"""

import base64
import re
import shlex
from unittest.mock import MagicMock, patch

import pytest


def node(instance_id, group="login-group", status="Running"):
    return {
        "InstanceId": instance_id,
        "InstanceGroupName": group,
        "InstanceStatus": {"Status": status},
    }


def sagemaker_listing(*nodes, hostname=True):
    """A boto3 sagemaker client whose ListClusterNodes returns `nodes`, and whose
    DescribeClusterNode gives each one a hostname unless `hostname` is False."""
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {"ClusterNodeSummaries": list(nodes)}
    ]
    client.describe_cluster_node.side_effect = lambda ClusterName, NodeId: {
        "NodeDetails": {"InstanceId": NodeId}
        | ({"PrivateDnsHostname": f"{NodeId}.internal"} if hostname else {})
    }
    return client


def resolve(transport, *nodes, hostname=True):
    client = sagemaker_listing(*nodes, hostname=hostname)
    with patch.object(transport.boto3, "client", return_value=client):
        return transport.resolve_login_node("cluster", "login-group")


def test_the_running_login_node_is_chosen(submit):
    assert resolve(submit.transport, node("i-1")) == "i-1.internal"


def test_nodes_in_other_groups_are_ignored(submit):
    """The API filter is a substring match, so `login-group-2` also comes back."""
    found = resolve(
        submit.transport,
        node("i-0", group="login-group-2"),
        node("i-1"),
    )
    assert found == "i-1.internal"


def test_nodes_that_are_not_running_are_ignored(submit):
    found = resolve(submit.transport, node("i-0", status="Pending"), node("i-1"))
    assert found == "i-1.internal"


def test_several_running_nodes_resolve_to_the_same_one_every_time(submit):
    """Sorted by instance id, so concurrent invocations agree."""
    assert resolve(submit.transport, node("i-2"), node("i-1")) == "i-1.internal"


def test_no_running_login_node_raises(submit):
    with pytest.raises(RuntimeError, match="no Running node"):
        resolve(submit.transport, node("i-1", status="Pending"))


def test_a_node_without_an_address_raises(submit):
    with pytest.raises(RuntimeError, match="no PrivateDnsHostname"):
        resolve(submit.transport, node("i-1"), hostname=False)


def test_an_unreachable_node_is_reported_as_a_network_fault(submit):
    """Distinguished from an auth failure, because the fix is somewhere else."""
    client = MagicMock()
    client.connect.side_effect = OSError("timed out")
    with (
        patch.object(submit.transport.paramiko, "SSHClient", return_value=client),
        pytest.raises(RuntimeError, match="port 22"),
    ):
        submit.transport.connect("i-1.internal", MagicMock())


def test_files_are_written_without_the_remote_shell_interpreting_them(submit):
    """Each payload is base64 inside one shell-quoted word, so nothing expands."""
    hostile = "$(rm -rf ~) `id` 'quote' \"dq\"\nEOF\n"
    client = MagicMock()
    ok = submit.transport.Result(0, "", "")
    with patch.object(submit.transport, "run", return_value=ok) as run:
        submit.transport.write_files(client, "/fsx/run dir", {"job.json": hostile})

    command = run.call_args[0][1]
    words = shlex.split(command)
    assert words[:3] == ["mkdir", "-p", "/fsx/run dir"]
    match = re.search(r"printf %s (\S+) \| base64 -d", command)
    assert match, command
    encoded = match.group(1)
    assert base64.b64decode(shlex.split(encoded)[0]).decode() == hostile


def test_a_failed_write_raises(submit):
    failed = submit.transport.Result(1, "", "No space left on device")
    with (
        patch.object(submit.transport, "run", return_value=failed),
        pytest.raises(RuntimeError, match="No space left"),
    ):
        submit.transport.write_files(MagicMock(), "/fsx/x", {"a": "b"})
