#!/usr/bin/env bash
set -euo pipefail

readonly MANAGEMENT_IP="10.82.36.1"
readonly SAMPLE_SECONDS=60
readonly COUNTERS=(
    rx_crc_errors_phy
    rx_symbol_err_phy
    rx_discards_phy
    link_down_events_phy
)

section() {
    printf '\n## %s\n' "$1"
}

counter_value() {
    local stats_text="$1"
    local counter_name="$2"

    awk -F: -v target="$counter_name" '
        {
            key = $1
            value = $2
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            if (key == target) {
                print value
                found = 1
                exit
            }
        }
        END {
            if (!found) {
                exit 1
            }
        }
    ' <<<"$stats_text"
}

interface_name="$(
    ip -o -4 addr show \
        | awk -v target="${MANAGEMENT_IP}/" 'index($4, target) == 1 { print $2; exit }'
)"
[[ -n "$interface_name" ]] || {
    printf 'ERROR: no interface owns %s\n' "$MANAGEMENT_IP" >&2
    exit 2
}

vendor_path="/sys/class/net/${interface_name}/device/vendor"
[[ -r "$vendor_path" ]] || {
    printf 'ERROR: interface %s has no readable PCI vendor\n' "$interface_name" >&2
    exit 3
}
[[ "$(cat "$vendor_path")" == "0x15b3" ]] || {
    printf 'ERROR: management interface %s is not a Mellanox device\n' "$interface_name" >&2
    exit 4
}

section "Identification"
date -Is
printf 'management_ip=%s\n' "$MANAGEMENT_IP"
printf 'interface=%s\n' "$interface_name"
printf 'pci_address=%s\n' "$(basename "$(readlink -f "/sys/class/net/${interface_name}/device")")"
ip -br link
lspci -nn | grep -i mellanox

section "Link settings (read-only)"
sudo ethtool "$interface_name"

section "First ethtool statistics sample"
first_stats="$(sudo ethtool -S "$interface_name")"
printf '%s\n' "$first_stats"

section "First ip statistics sample"
ip -s link show "$interface_name"

section "RDMA inventory"
rdma link 2>/dev/null || true
if command -v ibv_devinfo >/dev/null 2>&1; then
    ibv_devinfo 2>/dev/null || true
else
    printf 'ibv_devinfo is not installed\n'
fi

declare -A first_values=()
for counter_name in "${COUNTERS[@]}"; do
    first_values["$counter_name"]="$(counter_value "$first_stats" "$counter_name")" || {
        printf 'ERROR: required counter is missing: %s\n' "$counter_name" >&2
        exit 5
    }
done

section "Sampling interval"
printf 'Waiting %s seconds without changing link state or settings.\n' "$SAMPLE_SECONDS"
sleep "$SAMPLE_SECONDS"

section "Second ethtool statistics sample"
second_stats="$(sudo ethtool -S "$interface_name")"
printf '%s\n' "$second_stats"

section "Second ip statistics sample"
ip -s link show "$interface_name"

section "Counter deltas"
fault_present=0
for counter_name in "${COUNTERS[@]}"; do
    second_value="$(counter_value "$second_stats" "$counter_name")" || {
        printf 'ERROR: required counter disappeared: %s\n' "$counter_name" >&2
        exit 6
    }
    first_value="${first_values[$counter_name]}"
    delta=$((second_value - first_value))
    rate="$(awk -v change="$delta" -v seconds="$SAMPLE_SECONDS" \
        'BEGIN { printf "%.6f", change / seconds }')"
    printf '%s before=%s after=%s delta=%s rate_per_second=%s\n' \
        "$counter_name" "$first_value" "$second_value" "$delta" "$rate"
    if (( delta > 0 )); then
        fault_present=1
    fi
done

if [[ "$fault_present" -eq 1 ]]; then
    printf 'NETWORK PHYSICAL FAULT PRESENT\n'
    printf 'severity=P0\n'
    printf 'blocked_capabilities=production_NFS,second_node,cross_node_NCCL,high_speed_shared_storage\n'
    printf 'single_node_baseline_deployment=allowed\n'
else
    printf 'NETWORK PHYSICAL CHECK PASSED\n'
fi
