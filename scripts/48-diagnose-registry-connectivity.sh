#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:?RUN_ID is required}"
readonly diagnostic_scope="${DIAGNOSTIC_SCOPE:-full}"
readonly platform_root=/srv/gpu-platform/platform
readonly report_dir="${platform_root}/reports/network-${run_id}"
readonly management_ip=10.82.36.1

fail() {
  printf 'REGISTRY DIAGNOSTIC BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ -d "${report_dir}" ]] || fail "missing report directory ${report_dir}"
case "${diagnostic_scope}" in
  full|tls-only) ;;
  *) fail 'DIAGNOSTIC_SCOPE must be full or tls-only' ;;
esac

mgmt_if="$({
  ip -o -4 addr show \
    | awk -v target="${management_ip}/" 'index($4, target) == 1 {print $2; exit}'
})"
[[ -n "${mgmt_if}" ]] || fail "no interface owns ${management_ip}"

sanitize_one_line() {
  tr '\n,' ';;' | sed -E \
    -e 's/(Bearer[[:space:]]+)[A-Za-z0-9._~+\/-]+/\1<redacted>/g' \
    -e 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g'
}

print_proxy_value() {
  local source=$1
  local name=$2
  local value=$3
  local scheme rest authority host port userinfo

  if [[ "${name,,}" == no_proxy ]]; then
    printf '%s %s entries=%s value=%s\n' \
      "${source}" "${name}" \
      "$(( $(tr -cd ',' <<<"${value}" | wc -c) + 1 ))" "${value}"
    return
  fi

  scheme=unspecified
  rest=${value}
  if [[ "${rest}" == *://* ]]; then
    scheme=${rest%%://*}
    rest=${rest#*://}
  fi
  authority=${rest%%/*}
  userinfo=no
  if [[ "${authority}" == *@* ]]; then
    userinfo=yes
    authority=${authority##*@}
  fi
  host=${authority}
  port=default
  if [[ "${authority}" =~ ^\[([^]]+)\]:([0-9]+)$ ]]; then
    host="[${BASH_REMATCH[1]}]"
    port=${BASH_REMATCH[2]}
  elif [[ "${authority}" =~ ^([^:]+):([0-9]+)$ ]]; then
    host=${BASH_REMATCH[1]}
    port=${BASH_REMATCH[2]}
  fi
  printf '%s %s scheme=%s host=%s port=%s userinfo_present=%s\n' \
    "${source}" "${name}" "${scheme}" "${host}" "${port}" "${userinfo}"
}

write_proxy_audit() {
  local output="${report_dir}/proxy-audit.txt"
  local name value docker_environment docker_environment_files token

  {
    printf '===== current-shell =====\n'
    for name in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
      value=${!name-}
      if [[ -n "${value}" ]]; then
        print_proxy_value shell "${name}" "${value}"
      fi
    done

    printf '===== sudo-environment =====\n'
    while IFS='=' read -r name value; do
      case "${name}" in
        http_proxy|https_proxy|no_proxy|HTTP_PROXY|HTTPS_PROXY|NO_PROXY)
          print_proxy_value sudo "${name}" "${value}"
          ;;
      esac
    done < <(sudo env)

    printf '===== docker-service-environment =====\n'
    docker_environment="$(systemctl show docker -p Environment --value)"
    docker_environment_files="$(systemctl show docker -p EnvironmentFiles --value)"
    if [[ -z "${docker_environment}" ]]; then
      printf 'docker_environment=none\n'
    else
      for token in ${docker_environment}; do
        token=${token#\"}
        token=${token%\"}
        name=${token%%=*}
        value=${token#*=}
        case "${name}" in
          http_proxy|https_proxy|no_proxy|HTTP_PROXY|HTTPS_PROXY|NO_PROXY)
            print_proxy_value docker-service "${name}" "${value}"
            ;;
        esac
      done
    fi
    printf 'docker_environment_files=%s\n' \
      "${docker_environment_files:-none}"

    printf '===== proxy-config-file-names-only =====\n'
    sudo grep -RIlE \
      '(^|[^A-Za-z])(http_proxy|https_proxy|no_proxy|HTTP_PROXY|HTTPS_PROXY|NO_PROXY)[[:space:]]*=' \
      /etc/systemd/system/docker.service.d \
      /usr/lib/systemd/system/docker.service \
      /etc/environment \
      /etc/profile \
      /etc/profile.d \
      /etc/apt/apt.conf \
      /etc/apt/apt.conf.d \
      2>/dev/null || true
  } >"${output}"
  chmod 0640 "${output}"
}

write_dns_audit() {
  local system_output="${report_dir}/dns-system.txt"
  local repeat_output="${report_dir}/dns-repeat.csv"
  local domain type iteration answer rcode query_ms server dig_output
  local -a domains=(
    registry-1.docker.io
    auth.docker.io
    production.cloudflare.docker.com
    download.docker.com
    nvcr.io
    ghcr.io
  )
  local -a repeated_domains=(
    registry-1.docker.io
    auth.docker.io
    production.cloudflare.docker.com
  )

  {
    printf '===== resolv.conf =====\n'
    cat /etc/resolv.conf
    printf '===== resolvectl-status =====\n'
    resolvectl status
    printf '===== resolvectl-dns =====\n'
    resolvectl dns
    printf '===== resolvectl-domain =====\n'
    resolvectl domain
    for domain in "${domains[@]}"; do
      printf '===== getent %s =====\n' "${domain}"
      getent ahosts "${domain}" || true
    done
    for domain in registry-1.docker.io auth.docker.io production.cloudflare.docker.com; do
      for type in A AAAA; do
        printf '===== dig %s %s =====\n' "${domain}" "${type}"
        dig +tries=1 +time=5 "${domain}" "${type}" || true
      done
    done
  } >"${system_output}" 2>&1

  printf 'domain,type,iteration,rcode,query_ms,server,answers\n' \
    >"${repeat_output}"
  for domain in "${repeated_domains[@]}"; do
    for type in A AAAA; do
      for iteration in $(seq 1 10); do
        set +e
        dig_output="$(dig +tries=1 +time=5 "${domain}" "${type}" 2>&1)"
        set -e
        rcode="$(awk -F'status: ' '/status:/ {split($2,a,","); print a[1]; exit}' <<<"${dig_output}")"
        query_ms="$(awk '/Query time:/ {print $4; exit}' <<<"${dig_output}")"
        server="$(awk '/^;; SERVER:/ {print $3; exit}' <<<"${dig_output}")"
        answer="$(awk -v wanted="${type}" '$4 == wanted {printf "%s%s", separator, $5; separator=";"}' <<<"${dig_output}")"
        printf '%s,%s,%s,%s,%s,%s,%s\n' \
          "${domain}" "${type}" "${iteration}" "${rcode:-NO_RESPONSE}" \
          "${query_ms:-NA}" "${server:-NA}" "${answer:-none}" \
          >>"${repeat_output}"
      done
    done
  done
  chmod 0640 "${system_output}" "${repeat_output}"
}

curl_probe() {
  local family=$1
  local label=$2
  local url=$3
  local output=$4
  local metrics rc error_file error
  error_file="$(mktemp /tmp/h100-curl-error.XXXXXX)"
  set +e
  metrics="$(curl "-${family}" -sS -o /dev/null \
    --connect-timeout 10 --max-time 30 \
    -w '%{http_code},%{remote_ip},%{time_namelookup},%{time_connect},%{time_appconnect},%{time_total}' \
    "${url}" 2>"${error_file}")"
  rc=$?
  set -e
  error="$(sanitize_one_line <"${error_file}")"
  rm -f "${error_file}"
  printf '%s,IPv%s,%s,%s,%s,%s\n' \
    "${label}" "${family}" "${url}" "${rc}" "${metrics}" "${error:-none}" \
    >>"${output}"
}

write_http_matrix() {
  local output="${report_dir}/curl-endpoint-matrix.csv"
  printf 'label,family,url,exit,http_code,remote_ip,dns_seconds,tcp_seconds,tls_seconds,total_seconds,error\n' \
    >"${output}"
  curl_probe 4 docker-registry https://registry-1.docker.io/v2/ "${output}"
  curl_probe 6 docker-registry https://registry-1.docker.io/v2/ "${output}"
  curl_probe 4 docker-auth https://auth.docker.io/token "${output}"
  curl_probe 6 docker-auth https://auth.docker.io/token "${output}"
  curl_probe 4 docker-cdn https://production.cloudflare.docker.com/ "${output}"
  curl_probe 6 docker-cdn https://production.cloudflare.docker.com/ "${output}"
  curl_probe 4 docker-download https://download.docker.com/ "${output}"
  curl_probe 4 nvcr https://nvcr.io/v2/ "${output}"
  curl_probe 4 ghcr https://ghcr.io/v2/ "${output}"
  curl_probe 4 quay https://quay.io/v2/ "${output}"
  chmod 0640 "${output}"
}

write_tcp_matrix() {
  local output="${report_dir}/tcp-443.csv"
  local domain rc error_file error
  local -a domains=(
    registry-1.docker.io
    auth.docker.io
    production.cloudflare.docker.com
    download.docker.com
    nvcr.io
    ghcr.io
    quay.io
  )
  printf 'domain,port,exit,result\n' >"${output}"
  for domain in "${domains[@]}"; do
    error_file="$(mktemp /tmp/h100-nc-error.XXXXXX)"
    set +e
    nc -4 -vz -w 10 "${domain}" 443 >"${error_file}" 2>&1
    rc=$?
    set -e
    error="$(sanitize_one_line <"${error_file}")"
    rm -f "${error_file}"
    printf '%s,443,%s,%s\n' "${domain}" "${rc}" "${error:-none}" \
      >>"${output}"
  done
  chmod 0640 "${output}"
}

write_tls_audit() {
  local output="${report_dir}/tls-audit-v2.txt"
  local domain start_epoch end_epoch rc
  local -a domains=(
    registry-1.docker.io
    auth.docker.io
    production.cloudflare.docker.com
  )
  : >"${output}"
  for domain in "${domains[@]}"; do
    printf '===== %s =====\n' "${domain}" >>"${output}"
    start_epoch="$(date +%s)"
    set +e
    timeout 30 openssl s_client \
      -connect "${domain}:443" -servername "${domain}" -brief \
      </dev/null >>"${output}" 2>&1
    rc=$?
    set -e
    end_epoch="$(date +%s)"
    printf 'exit=%s elapsed_seconds=%s\n' "${rc}" "$((end_epoch - start_epoch))" \
      >>"${output}"
    if ((rc == 0)); then
      timeout 30 openssl s_client \
        -connect "${domain}:443" -servername "${domain}" -showcerts \
        </dev/null 2>/dev/null \
        | openssl x509 -noout -subject -issuer -dates \
        >>"${output}" 2>&1 || true
    fi
  done
  chmod 0640 "${output}"
}

write_registry_api_audit() {
  local output="${report_dir}/registry-api.txt"
  local header_file error_file auth_json token index_json index_rc auth_rc registry_rc
  local auth_start_epoch auth_end_epoch token_length amd64_digest platform_manifest config_digest
  local manifest_rc cdn_metrics cdn_rc
  header_file="$(mktemp /tmp/h100-registry-headers.XXXXXX)"
  error_file="$(mktemp /tmp/h100-registry-error.XXXXXX)"

  {
    printf '===== registry-v2 =====\n'
    set +e
    curl -4 -sS -D "${header_file}" -o /dev/null \
      --connect-timeout 10 --max-time 30 \
      https://registry-1.docker.io/v2/ 2>"${error_file}"
    registry_rc=$?
    set -e
    printf 'exit=%s\n' "${registry_rc}"
    awk 'BEGIN {IGNORECASE=1} /^HTTP\// || /^Docker-Distribution-Api-Version:/ || /^WWW-Authenticate:/ {print}' \
      "${header_file}"
    printf 'error='; sanitize_one_line <"${error_file}"; printf '\n'

    printf '===== anonymous-auth =====\n'
    auth_start_epoch="$(date +%s)"
    set +e
    auth_json="$(curl -4 -sS --connect-timeout 10 --max-time 30 \
      'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull' \
      2>"${error_file}")"
    auth_rc=$?
    set -e
    auth_end_epoch="$(date +%s)"
    printf 'exit=%s elapsed_seconds=%s json_object=%s token_field=%s access_token_field=%s\n' \
      "${auth_rc}" "$((auth_end_epoch - auth_start_epoch))" \
      "$(jq -r 'type == "object"' <<<"${auth_json}" 2>/dev/null || printf false)" \
      "$(jq -r 'has("token")' <<<"${auth_json}" 2>/dev/null || printf false)" \
      "$(jq -r 'has("access_token")' <<<"${auth_json}" 2>/dev/null || printf false)"
    printf 'error='; sanitize_one_line <"${error_file}"; printf '\n'

    if ((auth_rc == 0)); then
      token="$(jq -er '.token // .access_token' <<<"${auth_json}")"
      token_length=${#token}
      printf 'token_received=yes token_length=%s token_value_logged=no\n' \
        "${token_length}"

      set +e
      index_json="$(
        printf 'header = "Authorization: Bearer %s"\n' "${token}" \
          | curl --config - -4 -sS \
              -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
              --connect-timeout 10 --max-time 30 \
              https://registry-1.docker.io/v2/library/alpine/manifests/3.20.3 \
              2>"${error_file}"
      )"
      index_rc=$?
      set -e
      printf 'fixed_tag_manifest_exit=%s json_type=%s token_value_logged=no\n' \
        "${index_rc}" \
        "$(jq -r '.mediaType // type' <<<"${index_json}" 2>/dev/null || printf invalid)"
      printf 'error='; sanitize_one_line <"${error_file}"; printf '\n'

      if ((index_rc == 0)); then
        amd64_digest="$(jq -er '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest' \
          <<<"${index_json}" | head -n1)"
        set +e
        platform_manifest="$(
          printf 'header = "Authorization: Bearer %s"\n' "${token}" \
            | curl --config - -4 -sS \
                -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
                --connect-timeout 10 --max-time 30 \
                "https://registry-1.docker.io/v2/library/alpine/manifests/${amd64_digest}" \
                2>"${error_file}"
        )"
        manifest_rc=$?
        set -e
        printf 'amd64_manifest_exit=%s digest=%s\n' \
          "${manifest_rc}" "${amd64_digest}"
        if ((manifest_rc == 0)); then
          config_digest="$(jq -er '.config.digest' <<<"${platform_manifest}")"
          set +e
          cdn_metrics="$(
            printf 'header = "Authorization: Bearer %s"\n' "${token}" \
              | curl --config - -4 -sS -I -L -o /dev/null \
                  --connect-timeout 10 --max-time 30 \
                  -w 'http=%{http_code} remote=%{remote_ip} redirects=%{num_redirects} final_host=%{url.host} total=%{time_total}' \
                  "https://registry-1.docker.io/v2/library/alpine/blobs/${config_digest}" \
                  2>"${error_file}"
          )"
          cdn_rc=$?
          set -e
          printf 'cdn_config_blob_head_exit=%s %s token_value_logged=no\n' \
            "${cdn_rc}" "${cdn_metrics}"
          printf 'error='; sanitize_one_line <"${error_file}"; printf '\n'
        fi
      fi
      unset token auth_json index_json platform_manifest
    else
      printf 'token_received=no token_value_logged=no\n'
    fi
  } >"${output}" 2>&1
  chmod 0640 "${output}"
  rm -f "${header_file}" "${error_file}"
}

write_docker_audit() {
  local output="${report_dir}/docker-daemon-readonly.txt"
  {
    printf '===== docker-version =====\n'
    sudo docker version
    printf '===== docker-info =====\n'
    sudo docker info \
      | sed -E 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g'
    printf '===== docker-system-info =====\n'
    sudo docker system info 2>/dev/null \
      | sed -E 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g' \
      || true
    printf '===== daemon-json =====\n'
    sudo jq . /etc/docker/daemon.json \
      | sed -E 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g'
    printf '===== docker-status =====\n'
    systemctl status docker --no-pager
    printf '===== docker-unit =====\n'
    systemctl cat docker \
      | sed -E 's#(https?://)[^/@[:space:]]+@#\1<redacted>@#g'
    printf '===== docker-journal-filtered =====\n'
    sudo journalctl -u docker -b --no-pager \
      | grep -Ei 'timeout|registry|auth|TLS|proxy|DNS|IPv6|network unreachable|connection reset|context deadline exceeded|i/o timeout' \
      | sed -E 's/(Bearer[[:space:]]+)[A-Za-z0-9._~+\/-]+/\1<redacted>/g' \
      || true
  } >"${output}" 2>&1
  chmod 0640 "${output}"
}

write_mtu_audit() {
  local output="${report_dir}/mtu-path.txt"
  local target_ipv4 size
  target_ipv4="$(getent ahostsv4 registry-1.docker.io | awk '$2 == "STREAM" {print $1; exit}')"
  {
    printf 'target_ipv4=%s\n' "${target_ipv4:-none}"
    ip link show "${mgmt_if}"
    ip route get 1.1.1.1
    tracepath -n registry-1.docker.io 2>/dev/null || true
    tracepath -n auth.docker.io 2>/dev/null || true
    if [[ -n "${target_ipv4}" ]]; then
      for size in 1472 1400 1200; do
        printf '===== ping-size-%s =====\n' "${size}"
        ping -4 -c 3 -W 3 -M do -s "${size}" "${target_ipv4}" || true
      done
    fi
  } >"${output}" 2>&1
  chmod 0640 "${output}"
}

write_filter_audit() {
  local output="${report_dir}/filter-conntrack-readonly.txt"
  {
    printf '===== ufw =====\n'
    sudo ufw status verbose
    printf '===== nft =====\n'
    sudo nft list ruleset || true
    printf '===== iptables =====\n'
    sudo iptables-save || true
    printf '===== ip6tables =====\n'
    sudo ip6tables-save || true
    printf '===== forwarding =====\n'
    sysctl net.ipv4.ip_forward
    sysctl net.ipv6.conf.all.forwarding
    printf '===== conntrack =====\n'
    sysctl net.netfilter.nf_conntrack_count 2>/dev/null || true
    sysctl net.netfilter.nf_conntrack_max 2>/dev/null || true
    printf '===== socket-summary =====\n'
    ss -s
    printf '===== syn-sent =====\n'
    ss -tan state syn-sent
    printf '===== time-wait-head =====\n'
    ss -tan state time-wait | head
  } >"${output}" 2>&1
  chmod 0640 "${output}"
}

write_ip_inventory() {
  local output="${report_dir}/ip-inventory.txt"
  {
    ip -4 addr
    ip -6 addr
    ip -4 route
    ip -6 route
    ip rule
  } >"${output}" 2>&1
  chmod 0640 "${output}"
}

printf 'REGISTRY_DIAGNOSTIC_START=%s\n' "$(date -Is)"
if [[ "${diagnostic_scope}" == tls-only ]]; then
  write_tls_audit
  printf 'completed=tls-audit-v2\n'
  printf 'REGISTRY_DIAGNOSTIC_END=%s\n' "$(date -Is)"
  exit 0
fi
write_ip_inventory
printf 'completed=ip-inventory\n'
write_proxy_audit
printf 'completed=proxy-audit\n'
write_dns_audit
printf 'completed=dns-audit\n'
write_http_matrix
printf 'completed=http-matrix\n'
write_tcp_matrix
printf 'completed=tcp-matrix\n'
write_tls_audit
printf 'completed=tls-audit\n'
write_registry_api_audit
printf 'completed=registry-api\n'
write_docker_audit
printf 'completed=docker-audit\n'
write_mtu_audit
printf 'completed=mtu-audit\n'
write_filter_audit
printf 'completed=filter-audit\n'
printf 'REGISTRY_DIAGNOSTIC_END=%s\n' "$(date -Is)"
