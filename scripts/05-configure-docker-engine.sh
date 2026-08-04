#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
PLATFORM_ROOT=/srv/gpu-platform/platform
DESIRED_CONFIG="$PLATFORM_ROOT/config/docker-daemon.json"
BACKUP_DIR="$PLATFORM_ROOT/backups"
REPORT_DIR="$PLATFORM_ROOT/reports"
TARGET_CONFIG=/etc/docker/daemon.json

if [[ $(id -un) != codexops ]]; then
    printf '%s\n' 'ERROR: run this script as codexops.' >&2
    exit 1
fi

sudo -n true

for command_name in jq dockerd docker; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'ERROR: required command is missing: %s\n' "$command_name" >&2
        exit 1
    fi
done

if [[ ! -f $DESIRED_CONFIG ]]; then
    printf 'ERROR: desired Docker configuration is missing: %s\n' \
        "$DESIRED_CONFIG" >&2
    exit 1
fi

jq empty "$DESIRED_CONFIG"

sudo install -d -o codexops -g gpu-platform-admin -m 2770 \
    "$BACKUP_DIR" "$REPORT_DIR"
sudo install -d -o root -g root -m 0755 /etc/docker

temporary_directory=$(mktemp -d /tmp/h100-docker-config.XXXXXX)
cleanup() {
    sudo rm -rf -- "$temporary_directory"
}
trap cleanup EXIT
chmod 0700 "$temporary_directory"
candidate_config="$temporary_directory/daemon.json"

target_existed=no
if sudo test -e "$TARGET_CONFIG"; then
    target_existed=yes
    sudo cp -a "$TARGET_CONFIG" \
        "$BACKUP_DIR/daemon.json-${RUN_ID}"
    sudo jq -s '.[0] * .[1]' \
        "$TARGET_CONFIG" "$DESIRED_CONFIG" \
        >"$candidate_config"
else
    install -m 0644 "$DESIRED_CONFIG" "$candidate_config"
    printf 'ABSENT before run %s\n' "$RUN_ID" \
        >"$BACKUP_DIR/daemon.json-${RUN_ID}.absent"
    chmod 0660 "$BACKUP_DIR/daemon.json-${RUN_ID}.absent"
fi

jq empty "$candidate_config"
sudo dockerd --validate --config-file="$candidate_config"

sudo install -o root -g root -m 0644 \
    "$candidate_config" "$TARGET_CONFIG"

if ! sudo dockerd --validate --config-file="$TARGET_CONFIG"; then
    if [[ $target_existed == yes ]]; then
        sudo install -o root -g root -m 0644 \
            "$BACKUP_DIR/daemon.json-${RUN_ID}" "$TARGET_CONFIG"
    else
        sudo mv "$TARGET_CONFIG" \
            "$BACKUP_DIR/daemon.json-${RUN_ID}.invalid"
    fi
    printf '%s\n' 'ERROR: installed Docker configuration failed validation.' >&2
    exit 1
fi

sudo systemctl restart docker
sudo systemctl is-active --quiet docker

validation_log="$REPORT_DIR/docker-daemon-validation-${RUN_ID}.txt"
if [[ -e $validation_log ]]; then
    printf 'ERROR: validation report already exists: %s\n' \
        "$validation_log" >&2
    exit 1
fi

umask 0007
{
    printf '%s\n' '[daemon.json]'
    sudo jq . "$TARGET_CONFIG"
    printf '%s\n' '[dockerd validation]'
    sudo dockerd --validate --config-file="$TARGET_CONFIG"
    printf '%s\n' '[service]'
    systemctl is-enabled docker
    systemctl is-active docker
    printf '%s\n' '[docker info]'
    sudo docker info --format \
        'DockerRootDir={{.DockerRootDir}} LoggingDriver={{.LoggingDriver}} CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}} LiveRestore={{.LiveRestoreEnabled}} SecurityOptions={{json .SecurityOptions}}'
    printf '%s\n' '[listeners]'
    ss -lntup | grep -Ei 'docker|dockerd|:2375|:2376' || true
    printf '%s\n' '[identity and socket]'
    id codexops
    getent group docker
    stat -c '%n %U:%G %a %F' /run/docker.sock
} | tee "$validation_log"
chmod 0660 "$validation_log"

if sudo docker info --format '{{json .SecurityOptions}}' | grep -q 'name=userns'; then
    printf '%s\n' 'ERROR: user namespace remapping is unexpectedly enabled.' >&2
    exit 1
fi

if ss -lntup | grep -Eq ':(2375|2376)[[:space:]]'; then
    printf '%s\n' 'ERROR: a Docker TCP API listener is present.' >&2
    exit 1
fi

printf '%s\n' 'Docker daemon configuration validated.'
