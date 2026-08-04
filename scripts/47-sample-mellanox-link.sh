#!/usr/bin/env bash
set -euo pipefail

readonly management_ip="${MANAGEMENT_IP:-10.82.36.1}"
readonly run_id="${RUN_ID:?RUN_ID is required}"
readonly sample_count="${SAMPLE_COUNT:-6}"
readonly interval_seconds="${INTERVAL_SECONDS:-60}"
readonly platform_root=/srv/gpu-platform/platform
readonly report_dir="${REPORT_DIR:-${platform_root}/reports/network-${run_id}}"
stamp="$(date +%Y%m%d-%H%M%S)"
readonly stamp
readonly snapshot_dir="${report_dir}/mellanox-samples-${stamp}.d"
readonly samples_csv="${report_dir}/mellanox-samples-${stamp}.csv"
readonly intervals_csv="${report_dir}/mellanox-intervals-${stamp}.csv"
readonly metadata_file="${report_dir}/mellanox-sampling-${stamp}.txt"
readonly lock_file="${report_dir}/.mellanox-sampling.lock"

fail() {
  printf 'MELLANOX SAMPLING BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ "${sample_count}" =~ ^[0-9]+$ ]] || fail 'SAMPLE_COUNT must be an integer'
[[ "${interval_seconds}" =~ ^[0-9]+$ ]] || fail 'INTERVAL_SECONDS must be an integer'
((sample_count >= 2)) || fail 'at least two samples are required'
((interval_seconds >= 1)) || fail 'interval must be positive'
[[ "${report_dir}" == "${platform_root}/reports/"* ]] \
  || fail 'REPORT_DIR must be below the platform reports directory'
[[ -d "${report_dir}" ]] || fail "missing report directory ${report_dir}"

exec 9>"${lock_file}"
flock -n 9 || fail 'another Mellanox sampler is running'

interface="$({
  ip -o -4 addr show \
    | awk -v target="${management_ip}/" 'index($4, target) == 1 {print $2; exit}'
})"
[[ -n "${interface}" ]] || fail "no interface owns ${management_ip}"
[[ -r "/sys/class/net/${interface}/device/vendor" ]] \
  || fail "${interface} is not a PCI interface"
[[ "$(<"/sys/class/net/${interface}/device/vendor")" == 0x15b3 ]] \
  || fail "${interface} is not a Mellanox interface"

install -d -m 2770 "${snapshot_dir}"

ethtool_value() {
  local file=$1
  local key=$2
  awk -F: -v target="${key}" '
    {
      name=$1
      value=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (name == target && value ~ /^[0-9]+$/) {
        print value
        found=1
        exit
      }
    }
    END {if (!found) print "NA"}
  ' "${file}"
}

sysfs_value() {
  local name=$1
  local path="/sys/class/net/${interface}/statistics/${name}"
  [[ -r "${path}" ]] || fail "missing required interface statistic: ${name}"
  tr -d '[:space:]' <"${path}"
}

optional_sysfs_value() {
  local name=$1
  local path="/sys/class/net/${interface}/statistics/${name}"
  if [[ -r "${path}" ]]; then
    tr -d '[:space:]' <"${path}"
  else
    printf 'NA\n'
  fi
}

printf '%s\n' \
  'sample,epoch,iso,rx_bytes,tx_bytes,crc,symbol,corrected_bits,uncorrected,rx_discards,tx_discards,link_down,module_unplug,rx_errors,tx_errors,rx_missed_errors,rx_length_errors,rx_frame_errors,rx_over_errors,carrier_changes' \
  >"${samples_csv}"

{
  printf 'run_id=%s\n' "${run_id}"
  printf 'interface=%s\n' "${interface}"
  printf 'management_ip=%s\n' "${management_ip}"
  printf 'sample_count=%s\n' "${sample_count}"
  printf 'interval_seconds=%s\n' "${interval_seconds}"
  printf 'traffic_source=sysfs_interface_statistics\n'
  printf 'statistics_reset=no\n'
  printf 'link_configuration_changed=no\n'
  printf 'ethtool_counter_crc=rx_crc_errors_phy\n'
  printf 'ethtool_counter_symbol=rx_symbol_err_phy\n'
  printf 'ethtool_counter_corrected=rx_corrected_bits_phy\n'
  printf 'ethtool_counter_uncorrected=rx_uncorrected_phy\n'
  printf 'ethtool_counter_rx_discard=rx_discards_phy\n'
  printf 'ethtool_counter_tx_discard=tx_discards_phy\n'
  printf 'ethtool_counter_link_down=link_down_events_phy\n'
  printf 'ethtool_counter_module_unplug=module_unplug\n'
} >"${metadata_file}"

for ((sample=0; sample<sample_count; sample++)); do
  epoch="$(date +%s)"
  iso="$(date --iso-8601=seconds)"
  stats_file="${snapshot_dir}/sample-${sample}-ethtool.txt"
  ip_file="${snapshot_dir}/sample-${sample}-ip-link.txt"

  sudo ethtool -S "${interface}" | tee "${stats_file}" >/dev/null
  ip -s link show "${interface}" >"${ip_file}"

  rx_bytes="$(sysfs_value rx_bytes)"
  tx_bytes="$(sysfs_value tx_bytes)"
  crc="$(ethtool_value "${stats_file}" rx_crc_errors_phy)"
  symbol="$(ethtool_value "${stats_file}" rx_symbol_err_phy)"
  corrected_bits="$(ethtool_value "${stats_file}" rx_corrected_bits_phy)"
  uncorrected="$(ethtool_value "${stats_file}" rx_uncorrected_phy)"
  rx_discards="$(ethtool_value "${stats_file}" rx_discards_phy)"
  tx_discards="$(ethtool_value "${stats_file}" tx_discards_phy)"
  link_down="$(ethtool_value "${stats_file}" link_down_events_phy)"
  module_unplug="$(ethtool_value "${stats_file}" module_unplug)"

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "${sample}" "${epoch}" "${iso}" "${rx_bytes}" "${tx_bytes}" \
    "${crc}" "${symbol}" "${corrected_bits}" "${uncorrected}" \
    "${rx_discards}" "${tx_discards}" "${link_down}" "${module_unplug}" \
    "$(sysfs_value rx_errors)" "$(sysfs_value tx_errors)" \
    "$(sysfs_value rx_missed_errors)" "$(sysfs_value rx_length_errors)" \
    "$(sysfs_value rx_frame_errors)" "$(sysfs_value rx_over_errors)" \
    "$(optional_sysfs_value carrier_changes)" \
    >>"${samples_csv}"

  printf 'sample=%s/%s epoch=%s iso=%s crc=%s symbol=%s rx_bytes=%s\n' \
    "$((sample + 1))" "${sample_count}" "${epoch}" "${iso}" \
    "${crc}" "${symbol}" "${rx_bytes}"

  if ((sample + 1 < sample_count)); then
    sleep "${interval_seconds}"
  fi
done

awk -F, '
  function numeric(value) {
    return value ~ /^[0-9]+$/
  }
  function counter_delta(current, prior) {
    if (numeric(current) && numeric(prior)) return current-prior
    return "NA"
  }
  BEGIN {
    OFS=","
    print "interval,start_iso,end_iso,seconds,rx_bytes_delta,tx_bytes_delta,crc_delta,symbol_delta,rx_discard_delta,tx_discard_delta,discard_delta,link_down_delta,corrected_bits_delta,uncorrected_delta,module_unplug_delta,carrier_changes_delta,crc_per_second,symbol_per_second,crc_per_GB_received"
  }
  NR == 1 {next}
  NR == 2 {
    for (i=1; i<=NF; i++) previous[i]=$i
    next
  }
  {
    seconds=$2-previous[2]
    rx_delta=$4-previous[4]
    tx_delta=$5-previous[5]
    crc_delta=counter_delta($6, previous[6])
    symbol_delta=counter_delta($7, previous[7])
    rx_discard_delta=counter_delta($10, previous[10])
    tx_discard_delta=counter_delta($11, previous[11])
    if (numeric(rx_discard_delta) && numeric(tx_discard_delta)) {
      discard_delta=rx_discard_delta+tx_discard_delta
    } else {
      discard_delta="NA"
    }
    link_down_delta=counter_delta($12, previous[12])
    corrected_delta=counter_delta($8, previous[8])
    uncorrected_delta=counter_delta($9, previous[9])
    module_unplug_delta=counter_delta($13, previous[13])
    carrier_delta=counter_delta($20, previous[20])
    if (seconds > 0 && numeric(crc_delta) && numeric(symbol_delta)) {
      crc_rate=sprintf("%.6f", crc_delta/seconds)
      symbol_rate=sprintf("%.6f", symbol_delta/seconds)
    } else {
      crc_rate="NA"
      symbol_rate="NA"
    }
    if (rx_delta > 0 && numeric(crc_delta)) {
      crc_per_gb=sprintf("%.6f", crc_delta*1000000000/rx_delta)
    } else {
      crc_per_gb="NA"
    }
    print previous[1] "-" $1,previous[3],$3,seconds,rx_delta,tx_delta,crc_delta,symbol_delta,rx_discard_delta,tx_discard_delta,discard_delta,link_down_delta,corrected_delta,uncorrected_delta,module_unplug_delta,carrier_delta,crc_rate,symbol_rate,crc_per_gb
    for (i=1; i<=NF; i++) previous[i]=$i
  }
' "${samples_csv}" >"${intervals_csv}"

read -r total_crc total_symbol total_link < <(
  awk -F, '
    function numeric(value) {return value ~ /^[0-9]+$/}
    NR > 1 {
      if (numeric($7)) {crc+=$7; crc_seen=1}
      if (numeric($8)) {symbol+=$8; symbol_seen=1}
      if (numeric($12)) {link+=$12; link_seen=1}
    }
    END {
      print crc_seen ? crc : "NA", symbol_seen ? symbol : "NA", link_seen ? link : "NA"
    }
  ' \
    "${intervals_csv}"
)

chmod 0640 "${samples_csv}" "${intervals_csv}" "${metadata_file}"

printf 'samples_csv=%s\n' "${samples_csv}"
printf 'intervals_csv=%s\n' "${intervals_csv}"
printf 'snapshot_dir=%s\n' "${snapshot_dir}"
printf 'total_crc_delta=%s\n' "${total_crc}"
printf 'total_symbol_delta=%s\n' "${total_symbol}"
printf 'total_link_down_delta=%s\n' "${total_link}"

if [[ "${total_crc}" =~ ^[0-9]+$ && "${total_symbol}" =~ ^[0-9]+$ ]] \
  && ((total_crc > 0 || total_symbol > 0)); then
  printf 'PHYSICAL LINK ERROR CONFIRMED\n'
elif [[ "${total_crc}" == 0 && "${total_symbol}" == 0 ]]; then
  printf 'PHYSICAL LINK ERROR NOT REPRODUCED\n'
else
  printf 'PHYSICAL LINK SAMPLING INCOMPLETE: required counters unavailable\n'
fi
