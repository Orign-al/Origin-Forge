# Mellanox physical-link check

- Run ID: `20260804-050357`
- Management IP: `10.82.36.1`
- Interface: `ens1f0np0`
- PCI address: `0000:61:00.0`
- Adapter family: Mellanox ConnectX-6 Lx
- Link: detected, full duplex, autonegotiation enabled
- Negotiated speed: 10,000 Mb/s
- Sampling interval: 60 seconds

## Error counter deltas

| Counter | Before | After | Delta | Rate/second |
|---|---:|---:|---:|---:|
| `rx_crc_errors_phy` | 61,219,013 | 61,227,469 | 8,456 | 140.933333 |
| `rx_symbol_err_phy` | 61,218,604 | 61,227,060 | 8,456 | 140.933333 |
| `rx_discards_phy` | 0 | 0 | 0 | 0.000000 |
| `link_down_events_phy` | 1 | 1 | 0 | 0.000000 |

Kernel interface statistics also showed a large cumulative RX error count. The positive CRC and symbol-error deltas demonstrate an active physical-layer fault rather than only historical counters.

## RDMA state

- `mlx5_0/1`: ACTIVE / LINK_UP, netdev `ens1f0np0`
- `mlx5_1/1`: DOWN / DISABLED, netdev `ens1f1np1`
- `mlx5_2/1`: DOWN / DISABLED
- `ibv_devinfo`: not installed at the time of this check; base-package installation is pending

## Restrictions and status

- Severity: `P0`
- `NETWORK PHYSICAL FAULT PRESENT`
- Single-node baseline deployment may continue
- Production NFS is blocked
- Adding a second node is blocked
- Cross-node NCCL is blocked
- High-speed shared storage is blocked
- No speed, FEC, autonegotiation, link-state, firmware, switch, or network configuration changes were made

Raw report: `/srv/gpu-platform/platform/reports/network-physical-20260804-050357.txt`

Raw report SHA-256: `a72af067da724176565742664da7e86f8e35dd40ebe76349a1db48e1c48f415e`

Final status: `NETWORK PHYSICAL CHECK OPEN`
