#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
PLATFORM_ROOT=/srv/gpu-platform/platform
CONFIG_DIR="$PLATFORM_ROOT/config"
BACKUP_DIR="$PLATFORM_ROOT/backups"
REPORT_DIR="$PLATFORM_ROOT/reports"
KEY_URL=https://nvidia.github.io/libnvidia-container/gpgkey
LIST_URL=https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list
EXPECTED_FINGERPRINT=C95B321B61E88C1809C4F759DDCAE044F796ECB0
EXPECTED_VERSION=1.19.1-1
KEY_TARGET=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
LIST_TARGET=/etc/apt/sources.list.d/nvidia-container-toolkit.list
PIN_TARGET=/etc/apt/preferences.d/nvidia-container-toolkit

if [[ $(id -un) != codexops ]]; then
    printf '%s\n' 'ERROR: run this script as codexops.' >&2
    exit 1
fi

sudo -n true

for desired_file in \
    "$CONFIG_DIR/nvidia-container-toolkit.list" \
    "$CONFIG_DIR/nvidia-container-toolkit.pref"; do
    if [[ ! -f $desired_file ]]; then
        printf 'ERROR: required repository config is missing: %s\n' \
            "$desired_file" >&2
        exit 1
    fi
done

sudo install -d -o codexops -g gpu-platform-admin -m 2770 \
    "$BACKUP_DIR" "$REPORT_DIR"

temporary_directory=$(mktemp -d /tmp/h100-nvidia-container-repo.XXXXXX)
cleanup() {
    sudo rm -rf -- "$temporary_directory"
}
trap cleanup EXIT
chmod 0700 "$temporary_directory"
install -d -m 0700 "$temporary_directory/gnupg"

key_ascii="$temporary_directory/gpgkey"
key_binary="$temporary_directory/nvidia-container-toolkit-keyring.gpg"
official_list="$temporary_directory/official.list"
transformed_list="$temporary_directory/transformed.list"

curl -fsSL \
    --retry 5 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 55 \
    "$KEY_URL" \
    -o "$key_ascii"
curl -fsSL \
    --retry 5 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 55 \
    "$LIST_URL" \
    -o "$official_list"

actual_fingerprint=$(
    GNUPGHOME="$temporary_directory/gnupg" \
        gpg --batch --show-keys --with-colons "$key_ascii" \
        | awk -F: '$1 == "fpr" && fingerprint == "" {fingerprint=$10} END {print fingerprint}'
)
if [[ $actual_fingerprint != "$EXPECTED_FINGERPRINT" ]]; then
    printf 'ERROR: NVIDIA repository-key fingerprint mismatch: %s\n' \
        "$actual_fingerprint" >&2
    exit 1
fi

GNUPGHOME="$temporary_directory/gnupg" \
    gpg --batch --yes --dearmor --output "$key_binary" "$key_ascii"

sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    "$official_list" >"$transformed_list"

if ! cmp -s "$transformed_list" \
    "$CONFIG_DIR/nvidia-container-toolkit.list"; then
    printf '%s\n' 'ERROR: NVIDIA official production list differs from the reviewed repository config.' >&2
    diff -u "$CONFIG_DIR/nvidia-container-toolkit.list" \
        "$transformed_list" >&2 || true
    exit 1
fi

existing_files=()
for target in "$KEY_TARGET" "$LIST_TARGET" "$PIN_TARGET"; do
    if sudo test -e "$target"; then
        backup_name=$(basename "$target")
        sudo cp -a "$target" "$BACKUP_DIR/${backup_name}-${RUN_ID}"
        existing_files+=("$target")
    fi
done
if ((${#existing_files[@]} > 0)); then
    printf 'ERROR: existing NVIDIA Container Toolkit repository files were backed up but not overwritten: %s\n' \
        "${existing_files[*]}" >&2
    exit 1
fi

sudo install -o root -g root -m 0644 "$key_binary" "$KEY_TARGET"
sudo install -o root -g root -m 0644 \
    "$CONFIG_DIR/nvidia-container-toolkit.list" "$LIST_TARGET"
sudo install -o root -g root -m 0644 \
    "$CONFIG_DIR/nvidia-container-toolkit.pref" "$PIN_TARGET"

update_log="$REPORT_DIR/apt-update-nvidia-container-toolkit-${RUN_ID}.txt"
policy_log="$REPORT_DIR/nvidia-container-toolkit-policy-${RUN_ID}.txt"
if [[ -e $update_log || -e $policy_log ]]; then
    printf '%s\n' 'ERROR: NVIDIA Container Toolkit repository report already exists.' >&2
    exit 1
fi

umask 0007
sudo apt-get \
    -o Acquire::Retries=5 \
    -o Acquire::https::Timeout=20 \
    update 2>&1 | tee "$update_log"
apt-cache policy \
    nvidia-container-toolkit \
    nvidia-container-toolkit-base \
    libnvidia-container-tools \
    libnvidia-container1 \
    | tee "$policy_log"
chmod 0660 "$update_log" "$policy_log"

for package in \
    nvidia-container-toolkit \
    nvidia-container-toolkit-base \
    libnvidia-container-tools \
    libnvidia-container1; do
    candidate=$(
        apt-cache policy "$package" \
            | awk '$1 == "Candidate:" {candidate=$2} END {print candidate}'
    )
    if [[ $candidate != "$EXPECTED_VERSION" ]]; then
        printf 'ERROR: unexpected candidate for %s: %s\n' \
            "$package" "$candidate" >&2
        exit 1
    fi
done

if ! grep -qE '^[[:space:]]+1\.19\.1-1 700$' "$policy_log" \
    || ! grep -qE 'https://nvidia\.github\.io/libnvidia-container/stable/deb/' \
        "$policy_log"; then
    printf '%s\n' 'ERROR: nvidia.github.io or its effective version priority 700 was not present.' >&2
    exit 1
fi

if grep -qE '^[[:space:]]*deb .*experimental' "$LIST_TARGET"; then
    printf '%s\n' 'ERROR: experimental NVIDIA Container Toolkit repository is enabled.' >&2
    exit 1
fi

printf 'NVIDIA Container Toolkit repository configured; candidate=%s.\n' \
    "$EXPECTED_VERSION"
printf '%s\n' 'No NVIDIA Container Toolkit package was installed by this script.'
