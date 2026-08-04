#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
PLATFORM_ROOT=/srv/gpu-platform/platform
BACKUP_DIR="$PLATFORM_ROOT/backups"
REPORT_DIR="$PLATFORM_ROOT/reports"
DOCKER_REPO_URL=https://download.docker.com/linux/ubuntu
DOCKER_KEY_URL="$DOCKER_REPO_URL/gpg"
EXPECTED_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

if [[ $(id -un) != codexops ]]; then
    printf '%s\n' 'ERROR: run this script as codexops.' >&2
    exit 1
fi

sudo -n true

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != ubuntu || -z ${VERSION_CODENAME:-} ]]; then
    printf 'ERROR: unsupported or incomplete OS metadata: ID=%s VERSION_CODENAME=%s\n' \
        "${ID:-unset}" "${VERSION_CODENAME:-unset}" >&2
    exit 1
fi

architecture=$(dpkg --print-architecture)

if snap list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx docker; then
    printf '%s\n' 'ERROR: Snap Docker is installed; refusing to mix installations.' >&2
    exit 1
fi

conflicting_packages=(
    docker.io
    docker-compose
    docker-compose-v2
    docker-doc
    docker-buildx
    podman-docker
    containerd
    runc
)

installed_conflicts=()
for package in "${conflicting_packages[@]}"; do
    if dpkg-query -W -f='${db:Status-Status}\n' "$package" 2>/dev/null \
        | grep -qx 'installed'; then
        installed_conflicts+=("$package")
    fi
done

if ((${#installed_conflicts[@]} > 0)); then
    printf 'ERROR: conflicting packages are installed: %s\n' \
        "${installed_conflicts[*]}" >&2
    exit 1
fi

sudo install -d -o codexops -g gpu-platform-admin -m 2770 \
    "$BACKUP_DIR" "$REPORT_DIR"

temporary_directory=$(mktemp -d /tmp/h100-docker-repository.XXXXXX)
cleanup() {
    sudo rm -rf -- "$temporary_directory"
}
trap cleanup EXIT
chmod 0700 "$temporary_directory"

key_file="$temporary_directory/docker.asc"
source_file="$temporary_directory/docker.sources"
gpg_home="$temporary_directory/gnupg"
install -d -m 0700 "$gpg_home"

curl -fsSL \
    --retry 5 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 55 \
    "$DOCKER_KEY_URL" \
    -o "$key_file"

actual_fingerprint=$(
    GNUPGHOME="$gpg_home" gpg --batch --show-keys --with-colons "$key_file" \
        | awk -F: '$1 == "fpr" {print $10; exit}'
)

if [[ $actual_fingerprint != "$EXPECTED_FINGERPRINT" ]]; then
    printf 'ERROR: Docker signing-key fingerprint mismatch: %s\n' \
        "$actual_fingerprint" >&2
    exit 1
fi

curl -fsSI \
    --retry 5 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 55 \
    "$DOCKER_REPO_URL/dists/$VERSION_CODENAME/Release" \
    >/dev/null

printf '%s\n' \
    'Types: deb' \
    "URIs: $DOCKER_REPO_URL" \
    "Suites: $VERSION_CODENAME" \
    'Components: stable' \
    "Architectures: $architecture" \
    'Signed-By: /etc/apt/keyrings/docker.asc' \
    >"$source_file"

existing_repository_files=()
for existing_path in \
    /etc/apt/keyrings/docker.asc \
    /etc/apt/sources.list.d/docker.sources; do
    if sudo test -e "$existing_path"; then
        backup_name=$(basename "$existing_path")
        sudo cp -a "$existing_path" \
            "$BACKUP_DIR/${backup_name}-${RUN_ID}"
        existing_repository_files+=("$existing_path")
    fi
done

if ((${#existing_repository_files[@]} > 0)); then
    printf 'ERROR: existing Docker repository files were backed up but not overwritten: %s\n' \
        "${existing_repository_files[*]}" >&2
    exit 1
fi

sudo install -d -o root -g root -m 0755 /etc/apt/keyrings
sudo install -o root -g root -m 0644 \
    "$key_file" /etc/apt/keyrings/docker.asc
sudo install -o root -g root -m 0644 \
    "$source_file" /etc/apt/sources.list.d/docker.sources

update_log="$REPORT_DIR/apt-update-docker-repository-${RUN_ID}.txt"
policy_log="$REPORT_DIR/docker-package-policy-${RUN_ID}.txt"
if [[ -e $update_log || -e $policy_log ]]; then
    printf '%s\n' 'ERROR: Docker repository report already exists.' >&2
    exit 1
fi

sudo apt-get update 2>&1 | tee "$update_log"
apt-cache policy \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin \
    | tee "$policy_log"

chmod 0660 "$update_log" "$policy_log"

for package in \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin; do
    candidate=$(
        apt-cache policy "$package" \
            | awk '$1 == "Candidate:" {candidate=$2} END {print candidate}'
    )
    if [[ -z $candidate || $candidate == '(none)' ]]; then
        printf 'ERROR: no candidate version is available for %s.\n' \
            "$package" >&2
        exit 1
    fi
done

printf 'Docker repository configured for Ubuntu %s (%s).\n' \
    "$VERSION_ID" "$VERSION_CODENAME"
printf 'Verified Docker signing-key fingerprint: %s\n' \
    "$actual_fingerprint"
printf '%s\n' 'No Docker package was installed by this script.'
