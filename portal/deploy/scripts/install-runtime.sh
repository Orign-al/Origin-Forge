#!/usr/bin/env bash
set -euo pipefail

readonly PLATFORM_DIR=/srv/gpu-platform/platform
readonly SOURCE_DIR="${PLATFORM_DIR}/portal"
readonly RUNTIME_DIR=/opt/h100-portal
readonly UNIT_DIR=/etc/systemd/system
readonly CONFIG_DIR=/etc/h100-portal
readonly SSH_KEY_STAGING_DIR=/var/lib/h100-portal/ssh-key-staging
readonly PLATFORM_LIBRARY_DIR=/usr/local/lib/h100-platform
readonly PORTAL3F_ACCEPTANCE_SOURCE="${PLATFORM_DIR}/scripts/h100-origin-pilot-acceptance"
readonly PORTAL3F_GPU_PROBE_SOURCE="${PLATFORM_DIR}/tests/gpu-device-mapping/gpu-device-context-probe.c"
readonly COMPUTE_STAGE_SOURCE="${PLATFORM_DIR}/scripts/h100-provision-stage"
readonly PLATFORM_COMMON_SOURCE="${PLATFORM_DIR}/scripts/h100-platform-common.sh"
readonly WORKSPACE_ALIAS_SOURCE="${PLATFORM_DIR}/scripts/h100-workspace-alias"
readonly USER_CREATE_SOURCE="${PLATFORM_DIR}/scripts/h100-user-create"
readonly USER_GPU_ISOLATION_SOURCE="${PLATFORM_DIR}/scripts/h100-user-gpu-isolation"
readonly CONTAINER_CREATE_SOURCE="${PLATFORM_DIR}/scripts/h100-container-create"
readonly CONTAINER_START_SOURCE="${PLATFORM_DIR}/scripts/h100-container-start"
readonly CONTAINER_STOP_SOURCE="${PLATFORM_DIR}/scripts/h100-container-stop"
readonly CONTAINER_REBUILD_SOURCE="${PLATFORM_DIR}/scripts/h100-container-rebuild"
readonly CONTAINER_DELETE_SOURCE="${PLATFORM_DIR}/scripts/h100-container-delete"
readonly CONTAINER_STATUS_SOURCE="${PLATFORM_DIR}/scripts/h100-container-status"
readonly QUOTA_SHOW_SOURCE="${PLATFORM_DIR}/scripts/h100-quota-show"
readonly GPU_BYPASS_GUARD_SOURCE="${PLATFORM_DIR}/scripts/h100-gpu-bypass-guard"
readonly CONTAINER_GPU_RUNTIME_SOURCE="${PLATFORM_DIR}/scripts/h100-container-gpu-runtime"
readonly GPU_DEVELOPMENT_EPILOG_SOURCE="${PLATFORM_DIR}/scripts/h100-gpu-development-epilog"

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
[[ -f "$PLATFORM_COMMON_SOURCE" && ! -L "$PLATFORM_COMMON_SOURCE" ]] \
  || { echo "required deployment input missing: $PLATFORM_COMMON_SOURCE" >&2; exit 1; }
[[ -f "$WORKSPACE_ALIAS_SOURCE" && ! -L "$WORKSPACE_ALIAS_SOURCE" ]] \
  || { echo "required deployment input missing: $WORKSPACE_ALIAS_SOURCE" >&2; exit 1; }
[[ -f "$USER_CREATE_SOURCE" && ! -L "$USER_CREATE_SOURCE" ]] \
  || { echo "required deployment input missing: $USER_CREATE_SOURCE" >&2; exit 1; }
[[ -f "$USER_GPU_ISOLATION_SOURCE" && ! -L "$USER_GPU_ISOLATION_SOURCE" ]] \
  || { echo "required deployment input missing: $USER_GPU_ISOLATION_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_CREATE_SOURCE" && ! -L "$CONTAINER_CREATE_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_CREATE_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_START_SOURCE" && ! -L "$CONTAINER_START_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_START_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_STOP_SOURCE" && ! -L "$CONTAINER_STOP_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_STOP_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_REBUILD_SOURCE" && ! -L "$CONTAINER_REBUILD_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_REBUILD_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_DELETE_SOURCE" && ! -L "$CONTAINER_DELETE_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_DELETE_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_STATUS_SOURCE" && ! -L "$CONTAINER_STATUS_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_STATUS_SOURCE" >&2; exit 1; }
[[ -f "$QUOTA_SHOW_SOURCE" && ! -L "$QUOTA_SHOW_SOURCE" ]] \
  || { echo "required deployment input missing: $QUOTA_SHOW_SOURCE" >&2; exit 1; }
[[ -f "$GPU_BYPASS_GUARD_SOURCE" && ! -L "$GPU_BYPASS_GUARD_SOURCE" ]] \
  || { echo "required deployment input missing: $GPU_BYPASS_GUARD_SOURCE" >&2; exit 1; }
[[ -f "$CONTAINER_GPU_RUNTIME_SOURCE" && ! -L "$CONTAINER_GPU_RUNTIME_SOURCE" ]] \
  || { echo "required deployment input missing: $CONTAINER_GPU_RUNTIME_SOURCE" >&2; exit 1; }
[[ -f "$GPU_DEVELOPMENT_EPILOG_SOURCE" && ! -L "$GPU_DEVELOPMENT_EPILOG_SOURCE" ]] \
  || { echo "required deployment input missing: $GPU_DEVELOPMENT_EPILOG_SOURCE" >&2; exit 1; }

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

deployment_version="$(
  git -c safe.directory="$PLATFORM_DIR" \
    -C "$PLATFORM_DIR" rev-parse --verify 'HEAD^{commit}'
)"
[[ $deployment_version =~ ^[0-9a-f]{40}$ ]] \
  || { echo "deployment Git identity is invalid" >&2; exit 1; }
printf '%s\n' "$deployment_version" >"$RUNTIME_DIR/DEPLOYMENT_VERSION"
chown root:root "$RUNTIME_DIR/DEPLOYMENT_VERSION"
chmod 0444 "$RUNTIME_DIR/DEPLOYMENT_VERSION"

install -d -o root -g root -m 0755 \
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static" \
  "$RUNTIME_DIR/scripts" \
  "$RUNTIME_DIR/tests/gpu-device-mapping"
rsync -a --chown=root:root --chmod=Fgo-w,Dgo-w \
  "$SOURCE_DIR/apps/web/.next/static/" \
  "$RUNTIME_DIR/apps/web/.next/standalone/apps/web/.next/static/"

"$RUNTIME_DIR/venv/bin/pip" install --no-deps --no-build-isolation "$RUNTIME_DIR"
"$RUNTIME_DIR/venv/bin/pip" check

# Publish the approved daemon image once per platform deployment as an
# immutable, root-owned OCI layout. Provision attempts only validate and
# consume this local artifact; they never convert, pull, or resolve a registry
# reference. A publish failure stops deployment before the new Stage handler
# and integrity manifest are installed.
"$RUNTIME_DIR/venv/bin/h100-portal-local-image" publish \
  --deployment-version "$deployment_version"

# Activate accepts UUID key records from this Worker-only directory. The API
# never passes an arbitrary host path and cannot write this root-owned tree.
install -d -o root -g root -m 0700 "${SSH_KEY_STAGING_DIR}"

# Workspace V4 scripts source this exact library. Publish the library and
# alias handler before the Stage handler and integrity manifest can refer to
# them, so an interrupted deployment remains fail closed.
install -d -o root -g gpu-platform-admin -m 0750 "${PLATFORM_LIBRARY_DIR}"
install -o root -g gpu-platform-admin -m 0640 \
  "$PLATFORM_COMMON_SOURCE" \
  "${PLATFORM_LIBRARY_DIR}/h100-platform-common.sh"
install -o root -g gpu-platform-admin -m 0750 \
  "$WORKSPACE_ALIAS_SOURCE" \
  /usr/local/sbin/h100-workspace-alias
install -o root -g gpu-platform-admin -m 0750 \
  "$USER_CREATE_SOURCE" \
  /usr/local/sbin/h100-user-create
install -o root -g gpu-platform-admin -m 0750 \
  "$USER_GPU_ISOLATION_SOURCE" \
  /usr/local/sbin/h100-user-gpu-isolation

# Resource recycle uses this fixed root-owned artifact. Install it before the
# matching manifest: any interrupted deployment therefore fails closed on an
# integrity mismatch instead of executing an unbound lifecycle script.
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_CREATE_SOURCE" \
  /usr/local/sbin/h100-container-create
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_START_SOURCE" \
  /usr/local/sbin/h100-container-start
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_STOP_SOURCE" \
  /usr/local/sbin/h100-container-stop
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_REBUILD_SOURCE" \
  /usr/local/sbin/h100-container-rebuild
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_DELETE_SOURCE" \
  /usr/local/sbin/h100-container-delete
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_STATUS_SOURCE" \
  /usr/local/sbin/h100-container-status
install -o root -g gpu-platform-admin -m 0750 \
  "$QUOTA_SHOW_SOURCE" \
  /usr/local/sbin/h100-quota-show
install -o root -g gpu-platform-admin -m 0750 \
  "$GPU_BYPASS_GUARD_SOURCE" \
  /usr/local/sbin/h100-gpu-bypass-guard
install -o root -g gpu-platform-admin -m 0750 \
  "$CONTAINER_GPU_RUNTIME_SOURCE" \
  /usr/local/sbin/h100-container-gpu-runtime
install -o root -g root -m 0750 \
  "$GPU_DEVELOPMENT_EPILOG_SOURCE" \
  /usr/local/sbin/h100-gpu-development-epilog

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
  h100-portal-lease-expiry.timer \
  h100-portal-provision-reconcile.service \
  h100-portal-provision-reconcile.timer; do
  install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/$unit" "$UNIT_DIR/$unit"
done

systemctl daemon-reload
echo "Portal runtime and units installed; services were not enabled or started."
