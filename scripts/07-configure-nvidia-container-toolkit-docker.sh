#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
PLATFORM_ROOT=/srv/gpu-platform/platform
EXPECTED_CONFIG="$PLATFORM_ROOT/config/docker-daemon.json"
BACKUP_DIR="$PLATFORM_ROOT/backups"
REPORT_DIR="$PLATFORM_ROOT/reports"
TARGET_CONFIG=/etc/docker/daemon.json

if [[ $(id -un) != codexops ]]; then
    printf '%s\n' 'ERROR: run this script as codexops.' >&2
    exit 1
fi

sudo -n true

for command_name in jq dockerd docker nvidia-ctk; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'ERROR: required command is missing: %s\n' "$command_name" >&2
        exit 1
    fi
done

if ! sudo test -f "$TARGET_CONFIG"; then
    printf 'ERROR: Docker daemon config is missing: %s\n' \
        "$TARGET_CONFIG" >&2
    exit 1
fi
if [[ ! -f $EXPECTED_CONFIG ]]; then
    printf 'ERROR: reviewed expected config is missing: %s\n' \
        "$EXPECTED_CONFIG" >&2
    exit 1
fi

jq empty "$EXPECTED_CONFIG"
sudo jq empty "$TARGET_CONFIG"

daemon_backup="$BACKUP_DIR/daemon.json-before-nvidia-ctk-${RUN_ID}"
dry_run_log="$REPORT_DIR/nvidia-ctk-docker-dry-run-${RUN_ID}.txt"
validation_log="$REPORT_DIR/nvidia-ctk-docker-validation-${RUN_ID}.txt"
for path in "$daemon_backup" "$dry_run_log" "$validation_log"; do
    if sudo test -e "$path"; then
        printf 'ERROR: target already exists: %s\n' "$path" >&2
        exit 1
    fi
done

sudo cp -a "$TARGET_CONFIG" "$daemon_backup"

umask 0007
sudo nvidia-ctk runtime configure --runtime=docker --dry-run \
    >"$dry_run_log" 2>&1
chmod 0660 "$dry_run_log"

sudo nvidia-ctk runtime configure --runtime=docker

restore_daemon() {
    sudo install -o root -g root -m 0644 "$daemon_backup" "$TARGET_CONFIG"
    sudo systemctl restart docker || true
}

if ! sudo jq empty "$TARGET_CONFIG" \
    || ! sudo dockerd --validate --config-file="$TARGET_CONFIG"; then
    restore_daemon
    printf '%s\n' 'ERROR: nvidia-ctk produced an invalid Docker config; backup restored.' >&2
    exit 1
fi

temporary_directory=$(mktemp -d /tmp/h100-nvidia-ctk-config.XXXXXX)
cleanup() {
    sudo rm -rf -- "$temporary_directory"
}
trap cleanup EXIT
sudo jq -S . "$TARGET_CONFIG" >"$temporary_directory/installed.json"
jq -S . "$EXPECTED_CONFIG" >"$temporary_directory/expected.json"

if ! cmp -s "$temporary_directory/installed.json" \
    "$temporary_directory/expected.json"; then
    diff -u "$temporary_directory/expected.json" \
        "$temporary_directory/installed.json" >&2 || true
    restore_daemon
    printf '%s\n' 'ERROR: nvidia-ctk output differed from the reviewed expected config; backup restored.' >&2
    exit 1
fi

if ! sudo systemctl restart docker; then
    restore_daemon
    printf '%s\n' 'ERROR: Docker restart failed; pre-Toolkit config restored.' >&2
    exit 1
fi
sudo systemctl is-active --quiet docker

{
    printf '%s\n' '[versions]'
    nvidia-ctk --version
    printf '%s\n' '[daemon config]'
    sudo jq . "$TARGET_CONFIG"
    printf '%s\n' '[dockerd validation]'
    sudo dockerd --validate --config-file="$TARGET_CONFIG"
    printf '%s\n' '[Docker runtimes]'
    sudo docker info --format \
        'DefaultRuntime={{.DefaultRuntime}} Runtimes={{json .Runtimes}} DockerRootDir={{.DockerRootDir}} LoggingDriver={{.LoggingDriver}} LiveRestore={{.LiveRestoreEnabled}} SecurityOptions={{json .SecurityOptions}}'
    printf '%s\n' '[CDI devices]'
    sudo nvidia-ctk cdi list
    printf '%s\n' '[listeners]'
    ss -lntup | grep -Ei 'docker|dockerd|:2375|:2376' || true
} 2>&1 | tee "$validation_log"
chmod 0660 "$validation_log"

default_runtime=$(sudo docker info --format '{{.DefaultRuntime}}')
if [[ $default_runtime != runc ]]; then
    restore_daemon
    printf 'ERROR: unexpected Docker default runtime: %s\n' \
        "$default_runtime" >&2
    exit 1
fi

if sudo docker info --format '{{json .SecurityOptions}}' | grep -q 'name=userns'; then
    restore_daemon
    printf '%s\n' 'ERROR: user namespace remapping is unexpectedly enabled.' >&2
    exit 1
fi

if ss -lntup | grep -Eq ':(2375|2376)[[:space:]]'; then
    restore_daemon
    printf '%s\n' 'ERROR: a Docker TCP API listener is present.' >&2
    exit 1
fi

printf '%s\n' 'NVIDIA Container Toolkit Docker runtime configuration validated.'
