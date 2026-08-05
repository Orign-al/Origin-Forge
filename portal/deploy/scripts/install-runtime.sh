#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_DIR=/srv/gpu-platform/platform/portal
readonly RUNTIME_DIR=/opt/h100-portal
readonly UNIT_DIR=/etc/systemd/system
readonly CONFIG_DIR=/etc/h100-portal

if [[ ${EUID} -ne 0 ]]; then
  echo "install-runtime.sh must run as root" >&2
  exit 1
fi

for required in "$SOURCE_DIR" "$RUNTIME_DIR/venv" "$CONFIG_DIR/portal.env"; do
  if [[ ! -e $required ]]; then
    echo "required deployment input missing: $required" >&2
    exit 1
  fi
done

rsync -a --chown=root:root --chmod=Fgo-w,Dgo-w \
  --exclude=/node_modules/ \
  --exclude=/apps/web/node_modules/ \
  --exclude=/packages/*/node_modules/ \
  --exclude=venv \
  --exclude=/build/ \
  --exclude=h100_portal.egg-info \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  --exclude=playwright-report \
  --exclude=test-results \
  "$SOURCE_DIR/" "$RUNTIME_DIR/"

install -d -o root -g root -m 0755 \
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static"
rsync -a --chown=root:root --chmod=Fgo-w,Dgo-w \
  "$SOURCE_DIR/apps/web/.next/static/" \
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static/"

"$RUNTIME_DIR/venv/bin/pip" install --no-deps --no-build-isolation "$RUNTIME_DIR"
"$RUNTIME_DIR/venv/bin/pip" check

install -o root -g root -m 0640 \
  "$SOURCE_DIR/deploy/worker-scripts.json" \
  "$CONFIG_DIR/worker-scripts.json"

for unit in \
  h100-portal-worker.socket \
  h100-portal-worker.service \
  h100-portal-api.service \
  h100-portal-web.service; do
  install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/$unit" "$UNIT_DIR/$unit"
done

systemctl daemon-reload
echo "Portal runtime and units installed; services were not enabled or started."
