#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly context_dir="${platform_root}/templates/dev-container"
readonly base_image='public.ecr.aws/ubuntu/ubuntu:24.04@sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467'
readonly base_digest='sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467'
readonly image='h100-local/dev-container:ubuntu24.04-codexops-20260804'
readonly user_name=codexops

stamp="$(date +%Y%m%d-%H%M%S-%N)"
build_log="${platform_root}/logs/dev-image-build-${stamp}.log"
report_file="${platform_root}/reports/dev-image-build-${run_id}.md"
package_manifest="${platform_root}/reports/dev-image-packages-${run_id}.txt"

fail() {
  printf 'DEV IMAGE BUILD BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'

for file in Dockerfile sshd_config entrypoint.sh healthcheck.sh .dockerignore; do
  [[ -f "${context_dir}/${file}" ]] \
    || fail "missing build-context file: ${file}"
done
bash -n "${context_dir}/entrypoint.sh" "${context_dir}/healthcheck.sh"

[[ ! -e "${report_file}" ]] || fail "refusing to overwrite ${report_file}"
[[ ! -e "${package_manifest}" ]] || fail "refusing to overwrite ${package_manifest}"
if sudo docker image inspect "${image}" >/dev/null 2>&1; then
  fail "refusing to overwrite existing image tag: ${image}"
fi

sudo docker image inspect "${base_image}" \
  | jq -e --arg digest "${base_digest}" '
      .[0].Architecture == "amd64"
      and .[0].Os == "linux"
      and (.[0].RepoDigests | index("public.ecr.aws/ubuntu/ubuntu@" + $digest) != null)
    ' >/dev/null \
  || fail 'pinned Canonical Ubuntu base image is not present or does not match'

base_version="$({
  sudo docker run --rm \
    --network none \
    --entrypoint /bin/sh \
    "${base_image}" \
    -c '. /etc/os-release; printf "%s %s" "$NAME" "$VERSION_ID"'
} 2>/dev/null)"
[[ "${base_version}" == 'Ubuntu 24.04' ]] \
  || fail "unexpected base OS: ${base_version}"

dev_uid="$(id -u "${user_name}")"
dev_gid="$(id -g "${user_name}")"

if ! sudo docker build \
  --pull=false \
  --progress=plain \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "DEV_USER=${user_name}" \
  --build-arg "DEV_UID=${dev_uid}" \
  --build-arg "DEV_GID=${dev_gid}" \
  --tag "${image}" \
  "${context_dir}" \
  >"${build_log}" 2>&1; then
  tail -100 "${build_log}" >&2
  fail 'Docker image build failed'
fi

image_id="$(sudo docker image inspect --format '{{.Id}}' "${image}")"
image_size="$(sudo docker image inspect --format '{{.Size}}' "${image}")"
sudo docker image inspect "${image}" \
  | jq -e \
      --arg base_digest "${base_digest}" \
      --arg user_name "${user_name}" \
      --arg dev_uid "${dev_uid}" \
      --arg dev_gid "${dev_gid}" '
        .[0].Config.Labels["h100.base.digest"] == $base_digest
        and .[0].Config.Labels["h100.dev.user"] == $user_name
        and .[0].Config.Labels["h100.dev.uid"] == $dev_uid
        and .[0].Config.Labels["h100.dev.gid"] == $dev_gid
      ' >/dev/null \
  || fail 'built-image labels do not match expected values'

sudo docker run --rm \
  --network none \
  --entrypoint /bin/bash \
  "${image}" \
  -c '
    set -euo pipefail
    test "$(id -u codexops)" = 1001
    test "$(id -g codexops)" = 1001
    test "$(sudo -u codexops sudo -n id -u)" = 0
    test ! -e /usr/bin/docker
    test ! -e /usr/bin/nvidia-smi
    install -d -o root -g root -m 0700 /etc/ssh/persistent
    ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/persistent/ssh_host_ed25519_key
    ssh-keygen -q -t rsa -b 3072 -N "" -f /etc/ssh/persistent/ssh_host_rsa_key
    /usr/sbin/sshd -t
  '

sudo docker run --rm \
  --network none \
  --entrypoint /usr/bin/dpkg-query \
  "${image}" \
  -W '-f=${Package}\t${Version}\n' \
  | sort >"${package_manifest}"

mapfile -t slurm_states < <(sinfo -h -N -o '%T')
if ((${#slurm_states[@]} == 0)); then
  fail 'Slurm returned no node states after image build'
fi
for state in "${slurm_states[@]}"; do
  [[ "${state}" == drain* ]] \
    || fail "Slurm node is not drained after image build: ${state}"
done

{
  printf '# H100 long-lived development image build\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `DEV IMAGE BUILD PASSED`\n'
  printf -- '- Base publisher: `Canonical`\n'
  printf -- '- Base registry: `public.ecr.aws/ubuntu/ubuntu`\n'
  printf -- '- Base OS: `%s`\n' "${base_version}"
  printf -- '- Base reference: `%s`\n' "${base_image}"
  printf -- '- Local image: `%s`\n' "${image}"
  printf -- '- Local image ID: `%s`\n' "${image_id}"
  printf -- '- Image size bytes: `%s`\n' "${image_size}"
  printf -- '- Container user: `%s` (`%s:%s`)\n' "${user_name}" "${dev_uid}" "${dev_gid}"
  printf -- '- SSH passwords: disabled\n'
  printf -- '- Container sudo: passwordless for `%s`\n' "${user_name}"
  printf -- '- Docker daemon/CLI: not installed\n'
  printf -- '- Host NVIDIA driver: not installed\n'
  printf -- '- Package manifest: `%s`\n' "${package_manifest}"
  printf -- '- Build log: `%s`\n' "${build_log}"
  printf -- '- Slurm node remained DRAIN\n'
  printf -- '- MIG was not modified\n'
} >"${report_file}"

printf 'DEV IMAGE BUILD PASSED\n'
printf 'Image: %s\n' "${image}"
printf 'Image ID: %s\n' "${image_id}"
printf 'Report: %s\n' "${report_file}"
