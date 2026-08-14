#!/usr/bin/env bash
set -euo pipefail

readonly PLATFORM_DIR=/srv/gpu-platform/platform
readonly SOURCE_DIR="${PLATFORM_DIR}/portal"
readonly RUNTIME_DIR=/opt/h100-portal
readonly UNIT_DIR=/etc/systemd/system
readonly CONFIG_DIR=/etc/h100-portal
readonly SSH_KEY_STAGING_DIR=/var/lib/h100-portal/ssh-key-staging
readonly PORTAL3F_ACCEPTANCE_SOURCE="${PLATFORM_DIR}/scripts/h100-origin-pilot-acceptance"
readonly PORTAL3F_GPU_PROBE_SOURCE="${PLATFORM_DIR}/tests/gpu-device-mapping/gpu-device-context-probe.c"
readonly COMPUTE_STAGE_SOURCE="${PLATFORM_DIR}/scripts/h100-provision-stage"

if [[ ${EUID} -ne 0 ]]; then
  echo "install-runtime.sh must run as root" >&2
  exit 1
fi

for required in \
  "$SOURCE_DIR" \
  "$RUNTIME_DIR/venv" \
  "$CONFIG_DIR/portal.env" \
  "$PORTAL3F_ACCEPTANCE_SOURCE" \
  "$PORTAL3F_GPU_PROBE_SOURCE"; do
  if [[ ! -e $required ]]; then
    echo "required deployment input missing: $required" >&2
    exit 1
  fi
done

[[ -f "$COMPUTE_STAGE_SOURCE" && ! -L "$COMPUTE_STAGE_SOURCE" ]] \
  || { echo "required deployment input missing: $COMPUTE_STAGE_SOURCE" >&2; exit 1; }

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
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static" \
  "$RUNTIME_DIR/scripts" \
  "$RUNTIME_DIR/tests/gpu-device-mapping"
rsync -a --chown=root:root --chmod=Fgo-w,Dgo-w \
  "$SOURCE_DIR/apps/web/.next/static/" \
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static/"

"$RUNTIME_DIR/venv/bin/pip" install --no-deps --no-build-isolation "$RUNTIME_DIR"
"$RUNTIME_DIR/venv/bin/pip" check

# Activate accepts UUID key records from this Worker-only directory. The API
# never passes an arbitrary host path and cannot write this root-owned tree.
install -d -o root -g root -m 0700 "${SSH_KEY_STAGING_DIR}"

install -o root -g root -m 0640 \
  "$SOURCE_DIR/deploy/worker-scripts.json" \
  "$CONFIG_DIR/worker-scripts.json"

# Multi-user Stage is reachable only through the hash-pinned Worker handler.
# Installing this exact checked-in script here keeps code, manifest and host
# lifecycle implementation in one deployment transaction.
install -o root -g gpu-platform-admin -m 0750 \
  "$COMPUTE_STAGE_SOURCE" \
  /usr/local/sbin/h100-provision-stage

install -o root -g root -m 0755 \
  "$PORTAL3F_ACCEPTANCE_SOURCE" \
  "$RUNTIME_DIR/scripts/h100-origin-pilot-acceptance"

install -o root -g root -m 0644 \
  "$PORTAL3F_GPU_PROBE_SOURCE" \
  "$RUNTIME_DIR/tests/gpu-device-mapping/gpu-device-context-probe.c"

for unit in \
  h100-portal-worker.socket \
  h100-portal-worker.service \
  h100-portal-api.service \
  h100-portal-web.service \
  h100-portal-lease-expiry.service \
  h100-portal-lease-expiry.timer; do
  install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/$unit" "$UNIT_DIR/$unit"
done

systemctl daemon-reload
echo "Portal runtime and units installed; services were not enabled or started."
