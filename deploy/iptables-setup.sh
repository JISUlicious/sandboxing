#!/usr/bin/env bash
# deploy/iptables-setup.sh
#
# Idempotent egress lockdown for the sandbox bridge. SPEC-402, ARCH-041.
# Adds three rules to the DOCKER-USER chain (which Docker preserves
# across daemon restarts):
#
#   1. ACCEPT outbound traffic from the proxy container's IP.
#      (Squid handles the per-domain allowlist; the kernel just lets
#      the proxy reach the world.)
#   2. ACCEPT sandbox → proxy on TCP/${PROXY_PORT}.
#   3. DROP everything else originating from the sandbox subnet.
#      (No sandbox → sandbox, no sandbox → host, no direct egress.)
#
# Pre-conditions on the host (do these once before running the script):
#
#     # Pin the sandbox bridge to a known subnet:
#     docker network create \\
#         --subnet=$SANDBOX_SUBNET \\
#         --label sandbox.managed=true \\
#         sandbox_egress
#
#     # Run the proxy with a fixed IP:
#     docker run -d --name proxy \\
#         --network sandbox_egress --ip=$PROXY_IP \\
#         -v /opt/sandbox/proxy/allowed-domains.txt:/etc/squid/allowed-domains.txt:ro \\
#         sandbox-proxy:latest
#
# Run as root (or via sudo). Re-runs are safe — existing rules tagged
# `sandbox-egress` are removed first.

set -euo pipefail

SANDBOX_SUBNET="${SANDBOX_SUBNET:-172.30.0.0/24}"
PROXY_IP="${PROXY_IP:-172.30.0.2}"
PROXY_PORT="${PROXY_PORT:-3128}"
TAG="sandbox-egress"
CHAIN="DOCKER-USER"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)." >&2
    exit 1
fi

# DOCKER-USER appears the moment Docker boots. If the chain isn't there
# yet, the daemon hasn't started or Docker is too old.
if ! iptables -L "$CHAIN" -n >/dev/null 2>&1; then
    echo "ERROR: chain '$CHAIN' does not exist. Is Docker running?" >&2
    exit 1
fi

# Remove any rules previously inserted with our tag (idempotent on
# any number of duplicates). Each delete is retried until iptables
# returns non-zero — i.e. no more matching rules. Done this way
# instead of parsing `iptables -S` output because comments contain
# spaces and would word-split when re-fed via `iptables $rule`.
delete_all() {
    while iptables -D "$CHAIN" "$@" 2>/dev/null; do :; done
}
delete_all -s "$PROXY_IP" \
    -m comment --comment "$TAG proxy-egress" -j ACCEPT
delete_all -s "$SANDBOX_SUBNET" -d "$PROXY_IP" \
    -p tcp --dport "$PROXY_PORT" \
    -m comment --comment "$TAG sandbox-to-proxy" -j ACCEPT
delete_all -s "$SANDBOX_SUBNET" \
    -m comment --comment "$TAG sandbox-drop-default" -j DROP

# Append in order. Append, not insert: the chain is otherwise managed
# by Docker, and prepending would conflict with rules Docker inserts.
iptables -A "$CHAIN" \
    -s "$PROXY_IP" \
    -m comment --comment "$TAG proxy-egress" \
    -j ACCEPT

iptables -A "$CHAIN" \
    -s "$SANDBOX_SUBNET" -d "$PROXY_IP" -p tcp --dport "$PROXY_PORT" \
    -m comment --comment "$TAG sandbox-to-proxy" \
    -j ACCEPT

iptables -A "$CHAIN" \
    -s "$SANDBOX_SUBNET" \
    -m comment --comment "$TAG sandbox-drop-default" \
    -j DROP

echo "==> sandbox egress rules applied"
echo "    SANDBOX_SUBNET=$SANDBOX_SUBNET"
echo "    PROXY_IP=$PROXY_IP"
echo "    PROXY_PORT=$PROXY_PORT"
echo
echo "==> $CHAIN current state:"
iptables -L "$CHAIN" -nv --line-numbers
