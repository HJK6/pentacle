# v2 loopback alias — durable macOS install

The v2 bind coverage intentionally exercises both `127.0.0.1` and
`127.0.0.2` on one port. macOS does not treat every `127/8` address as usable
for a listener, so hosta needs the `127.0.0.2` alias on `lo0`.

This is a root `launchd` one-shot (`com.pentacle.loopback-alias`) rather than a
per-user agent: `ifconfig lo0 alias` requires administrator privilege. It is
idempotent, runs at boot, and exits after the alias is present.

## Install on hosta

Run from this directory in the deployed v2 checkout. The `sudo -n` form is a
safe preflight; if it reports that a password is required, rerun the install
commands in an interactive administrator shell rather than placing a password
in a launchd asset.

```sh
INSTALL_DIR=/usr/local/libexec/pentacle-v2
PLIST=/Library/LaunchDaemons/com.pentacle.loopback-alias.plist

sudo install -d -o root -g wheel -m 755 "$INSTALL_DIR"
sudo install -o root -g wheel -m 755 ensure_loopback_alias.sh "$INSTALL_DIR/ensure_loopback_alias.sh"
sed "s#__INSTALL_DIR__#${INSTALL_DIR}#g" com.pentacle.loopback-alias.plist \
  | sudo install -o root -g wheel -m 644 /dev/stdin "$PLIST"

sudo launchctl bootout system/com.pentacle.loopback-alias 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST"
sudo launchctl kickstart -k system/com.pentacle.loopback-alias

ifconfig lo0 | grep 'inet 127.0.0.2'
```

The `bootout` is scoped to this one label and is safe when the service is not
already loaded. Do not use `launchctl load` for the root LaunchDaemon on recent
macOS; `bootstrap` is the supported operation.

## Verify the gate prerequisite

```sh
services/chat-stream-v2/tools/gate_preflight.sh
```

A missing alias is reported as `loopback_alias_missing` with this README as the
remediation. The preflight is called automatically by `tools/smoke_gate.sh`;
run it before the full non-soak pytest command as well.

## Remove

Removal is not part of normal deploys. If the operator explicitly retires the
v2 multi-bind gate, unload the label and remove only the two installed files:

```sh
sudo launchctl bootout system/com.pentacle.loopback-alias
sudo rm /Library/LaunchDaemons/com.pentacle.loopback-alias.plist
sudo rm -rf /usr/local/libexec/pentacle-v2
```
