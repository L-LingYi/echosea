#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/opt/companion-system}"
echo "[1/3] Installing Companion System Host Updater Daemon for ${REPO_DIR}..."
sed "s|/opt/companion-system|${REPO_DIR}|g" "${REPO_DIR}/ops/updater/companion-updater.service" \
  > /etc/systemd/system/companion-updater.service

echo "[2/3] Reloading systemd and enabling companion-updater.service..."
systemctl daemon-reload
systemctl enable --now companion-updater.service

echo "[3/3] Checking updater health..."
sleep 1
systemctl status companion-updater.service --no-pager || true
