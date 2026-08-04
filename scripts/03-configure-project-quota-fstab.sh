#!/usr/bin/env bash
set -euo pipefail

readonly RUN_ID="20260804-050357"
readonly CANDIDATE_PATH="/tmp/fstab-${RUN_ID}.candidate"
readonly BACKUP_PATH="/srv/gpu-platform/platform/backups/fstab-${RUN_ID}"
readonly REPOSITORY_COPY="/srv/gpu-platform/platform/config/fstab"
readonly EXPECTED_BEFORE_SHA256="d4beb82b4bbc08558414d60cc674c8211af1a67dfb8e6d233df3e4906d3fd222"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

restore_fstab() {
    sudo install -o root -g root -m 0644 "$BACKUP_PATH" /etc/fstab
    sudo findmnt --verify --verbose || true
}

[[ "$(id -un)" == "codexops" ]] || die "must run as codexops"
[[ -f "$CANDIDATE_PATH" && ! -L "$CANDIDATE_PATH" ]] \
    || die "candidate fstab is missing or unsafe"
[[ "$(stat -c '%U' "$CANDIDATE_PATH")" == "codexops" ]] \
    || die "candidate fstab has an unexpected owner"
[[ ! -e "$BACKUP_PATH" ]] || die "timestamped backup path already exists"
[[ ! -e "$REPOSITORY_COPY" ]] || die "repository fstab copy already exists"

current_sha256="$(sha256sum /etc/fstab | awk '{ print $1 }')"
[[ "$current_sha256" == "$EXPECTED_BEFORE_SHA256" ]] \
    || die "/etc/fstab changed after audit; refusing to overwrite it"

for mountpoint in /var/lib/docker /srv/gpu-platform; do
    line_count="$(awk -v target="$mountpoint" '$1 !~ /^#/ && $2 == target { count++ } END { print count + 0 }' "$CANDIDATE_PATH")"
    [[ "$line_count" -eq 1 ]] || die "candidate must contain exactly one ${mountpoint} entry"
    awk -v target="$mountpoint" '
        $1 !~ /^#/ && $2 == target {
            if ($1 !~ /^\/dev\/disk\/by-id\/dm-uuid-LVM-/) exit 1
            if ($3 != "xfs") exit 1
            option_count = split($4, options, ",")
            have_noatime = 0
            have_prjquota = 0
            for (option_index = 1; option_index <= option_count; option_index++) {
                if (options[option_index] == "noatime") have_noatime = 1
                if (options[option_index] == "prjquota") have_prjquota = 1
            }
            if (!have_noatime || !have_prjquota) exit 1
            if ($5 != "0" || $6 != "1") exit 1
        }
    ' "$CANDIDATE_PATH" || die "candidate entry failed policy validation: ${mountpoint}"
done

sudo cp -a /etc/fstab "$BACKUP_PATH"
sudo install -o root -g root -m 0644 "$CANDIDATE_PATH" /etc/fstab

if ! sudo findmnt --verify --verbose; then
    printf 'findmnt validation failed; restoring %s\n' "$BACKUP_PATH" >&2
    restore_fstab
    rm -f -- "$CANDIDATE_PATH"
    die "fstab validation failed and the original was restored"
fi

installed_sha256="$(sha256sum /etc/fstab | awk '{ print $1 }')"
candidate_sha256="$(sha256sum "$CANDIDATE_PATH" | awk '{ print $1 }')"
if [[ "$installed_sha256" != "$candidate_sha256" ]]; then
    printf 'installed checksum differs from candidate; restoring backup\n' >&2
    restore_fstab
    rm -f -- "$CANDIDATE_PATH"
    die "fstab checksum validation failed and the original was restored"
fi

sudo install -o codexops -g gpu-platform-admin -m 0640 \
    /etc/fstab "$REPOSITORY_COPY"
rm -f -- "$CANDIDATE_PATH"

findmnt -no SOURCE,FSTYPE,OPTIONS /var/lib/docker
findmnt -no SOURCE,FSTYPE,OPTIONS /srv/gpu-platform
awk '$1 !~ /^#/ && ($2 == "/var/lib/docker" || $2 == "/srv/gpu-platform") { print }' /etc/fstab
printf 'QUOTA REBOOT REQUIRED\n'
