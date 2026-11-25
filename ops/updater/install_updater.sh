#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(realpath "${1:-/opt/companion-system}")
config=/etc/companion-updater.env
if [[ $EUID != 0 || ! $repo_dir =~ ^/[a-zA-Z0-9._/-]+$ || ! -d $repo_dir/.git ]]; then
  echo "Run as root with an existing deployment Git checkout." >&2
  exit 1
fi
if [[ ! -f $config || $(stat -c '%u:%a' "$config") != 0:600 ]]; then
  echo "Create /etc/companion-updater.env with owner root and mode 0600." >&2
  exit 1
fi
updater_dir="$repo_dir/ops/updater"
install -d -m 0755 /usr/local/libexec/companion-updater
install -m 0644 "$updater_dir/host_updater.py" /usr/local/libexec/companion-updater/host_updater.py
sed "s|/opt/companion-system|$repo_dir|g" "$updater_dir/companion-updater.service" |
  install -m 0644 /dev/stdin /etc/systemd/system/companion-updater.service
systemctl daemon-reload
systemctl enable companion-updater.service
systemctl restart companion-updater.service
systemctl is-active companion-updater.service
