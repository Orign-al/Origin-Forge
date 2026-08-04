#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

readonly run_id="${RUN_ID:?RUN_ID is required}"
readonly platform_root=/srv/gpu-platform/platform
readonly report_dir="${REPORT_DIR:-${platform_root}/reports/p0b-${run_id}}"

fail() {
  printf 'P0-B REGISTRY BASELINE BLOCKED: %s\n' "$*" >&2
  exit 1
}

sanitize_one_line() {
  tr '\n,' ';;' | sed -E \
    -e 's/(Bearer[[:space:]]+)[A-Za-z0-9._~+\/-]+/\1<redacted>/g' \
    -e 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g'
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
[[ "${report_dir}" == "${platform_root}/reports/"* ]] \
  || fail 'REPORT_DIR must be below the platform reports directory'
[[ -d "${report_dir}" ]] || fail "missing report directory: ${report_dir}"

readonly http_output="${report_dir}/current-registry-http.csv"
readonly tcp_output="${report_dir}/current-registry-tcp.csv"
readonly tls_output="${report_dir}/current-registry-tls.txt"
readonly -a labels=(registry auth cloudfront docker hub)
readonly -a domains=(
  registry-1.docker.io
  auth.docker.io
  production.cloudfront.docker.com
  docker.io
  hub.docker.com
)
readonly -a urls=(
  https://registry-1.docker.io/v2/
  https://auth.docker.io/token
  https://production.cloudfront.docker.com/
  https://docker.io/
  https://hub.docker.com/
)

printf 'timestamp,label,domain,url,curl_exit,http_code,remote_ip,dns_seconds,tcp_seconds,tls_seconds,total_seconds,error\n' \
  >"${http_output}"

for index in "${!domains[@]}"; do
  error_file="$(mktemp /tmp/h100-p0b-curl-error.XXXXXX)"
  set +e
  metrics="$(curl -4 -sS -o /dev/null \
    --connect-timeout 10 --max-time 30 \
    -w '%{http_code},%{remote_ip},%{time_namelookup},%{time_connect},%{time_appconnect},%{time_total}' \
    "${urls[index]}" 2>"${error_file}")"
  curl_exit=$?
  set -e
  error="$(sanitize_one_line <"${error_file}")"
  rm -f "${error_file}"
  printf '%s,%s,%s,%s,%s,%s,%s\n' \
    "$(date -Is)" "${labels[index]}" "${domains[index]}" "${urls[index]}" \
    "${curl_exit}" "${metrics}" "${error:-none}" >>"${http_output}"
done

printf 'timestamp,label,domain,port,nc_exit,result\n' >"${tcp_output}"
for index in "${!domains[@]}"; do
  error_file="$(mktemp /tmp/h100-p0b-nc-error.XXXXXX)"
  set +e
  nc -4 -vz -w 10 "${domains[index]}" 443 >"${error_file}" 2>&1
  nc_exit=$?
  set -e
  result="$(sanitize_one_line <"${error_file}")"
  rm -f "${error_file}"
  printf '%s,%s,%s,443,%s,%s\n' \
    "$(date -Is)" "${labels[index]}" "${domains[index]}" \
    "${nc_exit}" "${result:-none}" >>"${tcp_output}"
done

: >"${tls_output}"
for index in 0 1 2; do
  domain="${domains[index]}"
  printf '===== %s =====\n' "${domain}" >>"${tls_output}"
  start_epoch="$(date +%s)"
  set +e
  timeout 30 openssl s_client \
    -connect "${domain}:443" -servername "${domain}" -brief \
    </dev/null >>"${tls_output}" 2>&1
  tls_exit=$?
  set -e
  end_epoch="$(date +%s)"
  printf 'exit=%s elapsed_seconds=%s\n' \
    "${tls_exit}" "$((end_epoch - start_epoch))" >>"${tls_output}"
  if ((tls_exit == 0)); then
    timeout 30 openssl s_client \
      -connect "${domain}:443" -servername "${domain}" -showcerts \
      </dev/null 2>/dev/null \
      | openssl x509 -noout -subject -issuer -dates \
      >>"${tls_output}" 2>&1 || true
  fi
done

chmod 0640 "${http_output}" "${tcp_output}" "${tls_output}"
printf 'http_output=%s\ntcp_output=%s\ntls_output=%s\n' \
  "${http_output}" "${tcp_output}" "${tls_output}"
