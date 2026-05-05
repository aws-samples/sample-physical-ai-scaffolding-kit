#!/usr/bin/env python3
"""HyperPod lifecycle orchestrator.

Determines node type (controller/login/compute) and runs setup steps:
1. Mount FSx for Lustre at /fsx
2. Start Slurm daemons
3. Install Docker + Enroot + Pyxis for container support
"""

import argparse
import glob
import json
import os
import socket
import subprocess
import time


def discover_slurm_dir():
    """Find the Slurm installation directory.

    Prefer /opt/slurm (typically a symlink to the version the HyperPod agent
    manages, e.g. /opt/slurm-24.11). The glob fallback can pick the wrong
    install if multiple versions are staged on the node (e.g. /opt/slurm-25.11
    installed but not active), which produces version-mismatch RPC failures
    against slurmctld.
    """
    if os.path.exists("/opt/slurm"):
        slurm_dir = os.path.realpath("/opt/slurm")
        print(f"Discovered SLURM_DIR={slurm_dir} (via /opt/slurm symlink)")
        return slurm_dir
    for conf in sorted(glob.glob("/opt/slurm*/etc/slurm.conf")):
        slurm_dir = os.path.dirname(os.path.dirname(conf))
        print(f"Discovered SLURM_DIR={slurm_dir} (via glob)")
        return slurm_dir
    print("WARNING: /opt/slurm not found and no slurm.conf under /opt/slurm*/etc/")
    return "/opt/slurm"


def get_ip():
    """Get this node's private IP address."""
    for _ in range(5):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.254.254.254", 1))
            return s.getsockname()[0]
        except Exception:
            time.sleep(5)
        finally:
            s.close()
    raise RuntimeError("Could not determine IP address")


def run(script, *args, env=None):
    print(f">>> {script} {' '.join(str(a) for a in args)}")
    subprocess.run(
        ["sudo", "bash", script, *[str(a) for a in args]], check=True, env=env
    )


CONTROLLER_GROUP = "controller-machine"
LOGIN_GROUP = "login-group"


def detect_node_type(resource_config):
    """Return 'controller', 'login', or 'compute'."""
    ip = get_ip()
    for group in resource_config["InstanceGroups"]:
        for inst in group.get("Instances") or []:
            if inst.get("CustomerIpAddress") == ip:
                name = group["Name"]
                if name == CONTROLLER_GROUP:
                    return "controller"
                elif name == LOGIN_GROUP:
                    return "login"
                else:
                    return "compute"
    raise RuntimeError(f"This node ({ip}) not found in resource config")


def get_controller_ips(resource_config):
    for group in resource_config["InstanceGroups"]:
        if group["Name"] == CONTROLLER_GROUP:
            return [i["CustomerIpAddress"] for i in group.get("Instances") or []]
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-config", required=True)
    args = parser.parse_args()

    with open(args.resource_config) as f:
        resource_config = json.load(f)

    node_type = detect_node_type(resource_config)
    controller_ips = get_controller_ips(resource_config)
    print(f"Node type: {node_type}, Controller IPs: {controller_ips}")

    # Discover Slurm install path and export for all child scripts
    slurm_dir = discover_slurm_dir()
    env = {**os.environ, "SLURM_DIR": slurm_dir}

    # Each lifecycle script sources _lib.sh which auto-detects the node type
    # and self-guards — scripts that don't apply to this node exit 0 cleanly.
    # Run all scripts in a fixed order; each decides whether it's relevant.

    # 1. FSx dirs (controller-only; self-guarded)
    run("./create_fsx_dirs.sh")

    # 2. Configure Slurm accounting (controller-only; self-guarded). Runs
    #    before start_slurm.sh so that on fresh bootstrap, slurmctld's first
    #    start sees AccountingStorage* in slurm.conf and adopts the DBD's
    #    cluster_id from the start, avoiding the "CLUSTER ID MISMATCH" fatal
    #    that fires when slurmctld first boots without DBD config (generates
    #    a local cluster_id) and is later pointed at a DBD with a different
    #    cluster_id.
    run("./configure_slurm_accounting.sh", env=env)

    # 3. Start Slurm
    run("./start_slurm.sh", ",".join(controller_ips), env=env)

    # 4. Install Docker + Enroot + Pyxis
    run("./install_docker.sh", env=env)
    run("./install_enroot_pyxis.sh", env=env)

    # 5. Enable cgroup process tracking (must run after Slurm is started)
    run("./configure_slurm_cgroup.sh", env=env)

    # 6. Compute-node-only scripts (self-guarded)
    run("./register_slurm_features.sh", env=env)
    run("./install_xorg.sh", env=env)

    print("Lifecycle setup complete")


if __name__ == "__main__":
    main()
