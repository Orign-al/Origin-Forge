#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

readonly run_id="${RUN_ID:?RUN_ID is required}"
readonly platform_root=/srv/gpu-platform/platform
readonly report_dir="${REPORT_DIR:-${platform_root}/reports/p0b-${run_id}}"
readonly rounds="${DNS_ROUNDS:-10}"

fail() {
  printf 'P0-B DNS BASELINE BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
[[ "${report_dir}" == "${platform_root}/reports/"* ]] \
  || fail 'REPORT_DIR must be below the platform reports directory'
[[ -d "${report_dir}" ]] || fail "missing report directory: ${report_dir}"
[[ "${rounds}" =~ ^[0-9]+$ ]] || fail 'DNS_ROUNDS must be an integer'
((rounds >= 1 && rounds <= 20)) || fail 'DNS_ROUNDS must be between 1 and 20'

readonly system_output="${report_dir}/current-dns-system.txt"
readonly rounds_output="${report_dir}/current-dns-${rounds}-rounds.csv"
readonly -a formal_domains=(
  registry-1.docker.io
  auth.docker.io
  production.cloudfront.docker.com
)
readonly -a context_domains=(
  docker.io
  hub.docker.com
)
readonly -a query_types=(A AAAA)

{
  printf 'capture_time=%s\n' "$(date -Is)"
  printf 'resolver_path=current-system-only\n'
  printf 'approved_resolver_b=not_provided\n'
  printf '===== resolv.conf-link =====\n'
  readlink -f /etc/resolv.conf
  ls -l /etc/resolv.conf
  printf '===== resolv.conf =====\n'
  cat /etc/resolv.conf
  printf '===== resolvectl-status =====\n'
  resolvectl status
  printf '===== resolvectl-dns =====\n'
  resolvectl dns
  printf '===== resolvectl-domain =====\n'
  resolvectl domain
  for domain in "${formal_domains[@]}"; do
    printf '===== getent-ahosts %s =====\n' "${domain}"
    getent ahosts "${domain}" || true
  done
  for domain in "${context_domains[@]}"; do
    printf '===== getent-ahosts %s =====\n' "${domain}"
    getent ahosts "${domain}" || true
  done
} >"${system_output}" 2>&1

printf 'timestamp,round,domain,type,dig_exit,rcode,query_ms,server,cnames,ttls,answers\n' \
  >"${rounds_output}"

all_domains=("${formal_domains[@]}" "${context_domains[@]}")
for round in $(seq 1 "${rounds}"); do
  for domain in "${all_domains[@]}"; do
    for query_type in "${query_types[@]}"; do
      set +e
      dig_output="$(dig +tries=1 +time=5 "${domain}" "${query_type}" 2>&1)"
      dig_exit=$?
      set -e
      rcode="$(awk -F'status: ' '/status:/ {split($2,a,","); print a[1]; exit}' <<<"${dig_output}")"
      query_ms="$(awk '/Query time:/ {print $4; exit}' <<<"${dig_output}")"
      server="$(awk '/^;; SERVER:/ {print $3; exit}' <<<"${dig_output}")"
      cnames="$(awk '$3 == "IN" && $4 == "CNAME" {printf "%s%s->%s", separator, $1, $5; separator=";"}' <<<"${dig_output}")"
      ttls="$(awk -v wanted="${query_type}" '$3 == "IN" && $4 == wanted {printf "%s%s", separator, $2; separator=";"}' <<<"${dig_output}")"
      answers="$(awk -v wanted="${query_type}" '$3 == "IN" && $4 == wanted {printf "%s%s", separator, $5; separator=";"}' <<<"${dig_output}")"
      printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$(date -Is)" "${round}" "${domain}" "${query_type}" \
        "${dig_exit}" "${rcode:-NO_RESPONSE}" "${query_ms:-NA}" \
        "${server:-NA}" "${cnames:-none}" "${ttls:-none}" "${answers:-none}" \
        >>"${rounds_output}"
    done
  done
done

chmod 0640 "${system_output}" "${rounds_output}"
printf 'system_output=%s\nrounds_output=%s\n' "${system_output}" "${rounds_output}"
