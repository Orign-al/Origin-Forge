#!/usr/bin/env bash
set -euo pipefail

/usr/sbin/sshd -t
/usr/bin/ss -H -lnt 'sport = :22' | grep -q .
