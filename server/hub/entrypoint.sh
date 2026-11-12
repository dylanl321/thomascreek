#!/bin/sh
# Container entrypoint. When the built-in VPN is on, the hub routes tunnel
# traffic to the home LAN, so forwarding and NAT for the tunnel subnet are set
# up here (needs NET_ADMIN and host networking; see docs/DEPLOY.md).
set -e
if [ "${HUB_VPN_NAT:-0}" = "1" ] && [ -n "${HUB_VPN_SUBNET:-}" ]; then
  sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || echo "warning: could not enable ip_forward"
  iptables -t nat -C POSTROUTING -s "$HUB_VPN_SUBNET" -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s "$HUB_VPN_SUBNET" -j MASQUERADE
fi
if [ "$#" -gt 0 ]; then
  exec hub "$@"
fi
exec hub serve
