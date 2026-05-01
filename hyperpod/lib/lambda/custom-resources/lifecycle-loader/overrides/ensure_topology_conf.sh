#!/bin/bash
set -euo pipefail

ensure_topology_conf() {
  local candidate_confs=()
  local active_conf=""

  if command -v scontrol >/dev/null 2>&1; then
    active_conf="$(scontrol show config 2>/dev/null | awk -F= '/^SLURM_CONF/ { gsub(/[ \t]/, "", $2); print $2; exit }' || true)"
    if [[ -n "${active_conf:-}" ]]; then
      candidate_confs+=("${active_conf}")
    fi
  fi

  candidate_confs+=("/opt/slurm/etc/slurm.conf")
  while IFS= read -r conf; do
    candidate_confs+=("${conf}")
  done < <(find /opt -maxdepth 4 -path '/opt/slurm-*/etc/slurm.conf' -type f 2>/dev/null | sort)

  local slurm_conf=""
  for conf in "${candidate_confs[@]}"; do
    if [[ -f "${conf}" ]] && grep -q '^TopologyPlugin=topology/tree' "${conf}" && grep -q '^NodeName=' "${conf}"; then
      slurm_conf="${conf}"
      break
    fi
  done

  if [[ -z "${slurm_conf}" ]]; then
    echo "[WARN] No Slurm config with topology/tree and NodeName entries found; skipping topology guard"
    exit 0
  fi

  local topology_conf
  topology_conf="$(dirname "${slurm_conf}")/topology.conf"

  local nodes
  nodes="$(awk '
    /^NodeName=/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^NodeName=/) {
          node = $i
          sub(/^NodeName=/, "", node)
          if (node != "DEFAULT") {
            print node
          }
        }
      }
    }
  ' "${slurm_conf}" | paste -sd, -)"

  if [[ -z "${nodes}" ]]; then
    echo "[WARN] No NodeName entries found in ${slurm_conf}; leaving ${topology_conf} unchanged"
    exit 0
  fi

  local expected="SwitchName=switch0 Nodes=${nodes}"
  if [[ -f "${topology_conf}" ]] && grep -qxF "${expected}" "${topology_conf}"; then
    exit 0
  fi

  echo "[INFO] Writing ${topology_conf} for topology/tree from ${slurm_conf}: ${nodes}"
  printf '%s\n' "${expected}" > "${topology_conf}.tmp"
  mv "${topology_conf}.tmp" "${topology_conf}"
  chown slurm:slurm "${topology_conf}" || true
  chmod 600 "${topology_conf}" || true

  if systemctl is-active --quiet slurmctld; then
    scontrol reconfigure || echo "[WARN] scontrol reconfigure failed after topology.conf update"
  fi
}

install_systemd_guard() {
  local scripts_dir="/opt/slurm/etc/scripts"
  local helper="${scripts_dir}/ensure_topology_conf.sh"

  mkdir -p "${scripts_dir}"
  cp "$0" "${helper}"
  chmod +x "${helper}"

  cat > /etc/systemd/system/slurm-topology-guard.service <<EOF
[Unit]
Description=Ensure Slurm topology.conf is populated

[Service]
Type=oneshot
ExecStart=${helper} --run
EOF

  cat > /etc/systemd/system/slurm-topology-guard.path <<'EOF'
[Unit]
Description=Watch Slurm topology configuration

[Path]
PathChanged=/opt/slurm/etc/slurm.conf
PathChanged=/opt/slurm/etc/topology.conf
PathModified=/opt/slurm/etc
PathChanged=/opt/slurm-24.11/etc/slurm.conf
PathChanged=/opt/slurm-24.11/etc/topology.conf
PathModified=/opt/slurm-24.11/etc
PathChanged=/opt/slurm-25.11/etc/slurm.conf
PathChanged=/opt/slurm-25.11/etc/topology.conf
PathModified=/opt/slurm-25.11/etc
Unit=slurm-topology-guard.service

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/slurm-topology-guard.timer <<'EOF'
[Unit]
Description=Periodically ensure Slurm topology.conf is populated

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
Unit=slurm-topology-guard.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now slurm-topology-guard.path
  systemctl enable --now slurm-topology-guard.timer
  systemctl start slurm-topology-guard.service || true
}

if [[ "${1:-}" == "--run" ]]; then
  ensure_topology_conf
else
  install_systemd_guard
fi
