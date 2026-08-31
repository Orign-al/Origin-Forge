#!/usr/bin/env bash
set -euo pipefail

if (($# != 2)); then
  printf 'Usage: install-web-artifact.sh ARCHIVE WEB_SOURCE_DIR\n' >&2
  exit 2
fi

readonly ARCHIVE=$1
readonly WEB_SOURCE_DIR=$2
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly AUDITOR="${SCRIPT_DIR}/web_artifact.py"

[[ -f "${ARCHIVE}" && ! -L "${ARCHIVE}" ]] \
  || { printf 'Web artifact must be a real file\n' >&2; exit 1; }
[[ -d "${WEB_SOURCE_DIR}" && ! -L "${WEB_SOURCE_DIR}" ]] \
  || { printf 'Web source target must be a real directory\n' >&2; exit 1; }
[[ -f "${AUDITOR}" && ! -L "${AUDITOR}" ]] \
  || { printf 'Web artifact auditor is unavailable\n' >&2; exit 1; }

readonly TARGET_PARENT="$(cd -- "${WEB_SOURCE_DIR}/.." && pwd -P)"
readonly TARGET_NAME="$(basename -- "${WEB_SOURCE_DIR}")"
readonly TARGET_ROOT="${TARGET_PARENT}/${TARGET_NAME}"
[[ "${TARGET_ROOT}" != / && "${TARGET_ROOT}" != /srv && "${TARGET_ROOT}" != /opt ]] \
  || { printf 'Web source target is too broad\n' >&2; exit 1; }

stage="$(mktemp -d "${TARGET_PARENT}/.h100-web-artifact.XXXXXX")"
cleanup() {
  rm -rf -- "${stage}"
}
trap cleanup EXIT

install -m 0400 -- "${ARCHIVE}" "${stage}/artifact.tar.gz"
/usr/bin/python3 "${AUDITOR}" audit-archive "${stage}/artifact.tar.gz" --quiet
mkdir -m 0755 "${stage}/extract"
tar --extract --gzip --file "${stage}/artifact.tar.gz" \
  --directory "${stage}/extract" --no-same-owner --no-same-permissions
/usr/bin/python3 "${AUDITOR}" audit-tree "${stage}/extract/.next" --quiet

[[ -f "${stage}/extract/.next/standalone/apps/web/server.js" ]] \
  || { printf 'Web artifact entrypoint is unavailable\n' >&2; exit 1; }
[[ -d "${stage}/extract/.next/standalone/node_modules" \
  && ! -L "${stage}/extract/.next/standalone/node_modules" ]] \
  || { printf 'Web artifact dependency closure is unavailable\n' >&2; exit 1; }

if [[ -e "${TARGET_ROOT}/.next" || -L "${TARGET_ROOT}/.next" ]]; then
  [[ -d "${TARGET_ROOT}/.next" && ! -L "${TARGET_ROOT}/.next" ]] \
    || { printf 'Web .next target must be a real directory\n' >&2; exit 1; }
else
  mkdir -m 0755 "${TARGET_ROOT}/.next"
fi
rsync -a --delete \
  "${stage}/extract/.next/" \
  "${TARGET_ROOT}/.next/"
/usr/bin/python3 "${AUDITOR}" audit-tree "${TARGET_ROOT}/.next" --quiet

printf 'Web artifact installed safely: build=%s target=%s\n' \
  "$(<"${TARGET_ROOT}/.next/BUILD_ID")" "${TARGET_ROOT}/.next"
