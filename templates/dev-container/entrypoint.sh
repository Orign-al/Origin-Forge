#!/usr/bin/env bash
set -euo pipefail

readonly key_dir=/etc/ssh/persistent

install -d -o root -g root -m 0755 /run/sshd
install -d -o root -g root -m 0700 "${key_dir}"

if [[ ! -e "${key_dir}/ssh_host_ed25519_key" ]]; then
  ssh-keygen \
    -q \
    -t ed25519 \
    -N '' \
    -f "${key_dir}/ssh_host_ed25519_key"
fi
if [[ ! -e "${key_dir}/ssh_host_rsa_key" ]]; then
  ssh-keygen \
    -q \
    -t rsa \
    -b 3072 \
    -N '' \
    -f "${key_dir}/ssh_host_rsa_key"
fi

chown root:root "${key_dir}"/ssh_host_*_key "${key_dir}"/ssh_host_*_key.pub
chmod 0600 "${key_dir}"/ssh_host_*_key
chmod 0644 "${key_dir}"/ssh_host_*_key.pub

/usr/sbin/sshd -t
exec /usr/sbin/sshd -D -e
