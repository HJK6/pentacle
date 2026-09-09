#!/bin/sh
# Idempotent root launchd action for the v2 multi-bind loopback prerequisite.
set -eu

IFCONFIG="${IFCONFIG:-/sbin/ifconfig}"
ALIAS="127.0.0.2"

if "$IFCONFIG" lo0 2>/dev/null | awk -v address="${ALIAS}" '$1 == "inet" && $2 == address {found = 1} END {exit !found}'; then
  exit 0
fi

exec "$IFCONFIG" lo0 alias "$ALIAS" netmask 255.0.0.0
