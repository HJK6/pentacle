#!/bin/sh
# Per-launch Claude authentication-context prelude (generic, opt-in).
#
# This launcher obtains a sanctioned keychain credential from SSM, unlocks this
# launch context's login keychain, and replaces itself with the real Claude
# binary. It deliberately does no caching, retrying, or reauthentication. All
# host-specific values are supplied by configuration (environment), never
# hard-coded: a host installs it through its own gated profile change and sets
# PENTACLE_AUTH_SSM_PARAMETER (and optionally the keychain / claude paths).
set -eu
# A caller may invoke `sh -x`; turn tracing off before a credential can enter a
# shell variable. `set +x` itself is safe to trace.
set +x

# Cross-lane fixture: Lane 2's source contract test reads this exact literal and
# the `printf` shape below. Do not change either without the pinned E contract.
AUTH_CONTEXT_CODE="provider_auth_context_unavailable"
# Host-specific values come from configuration. Defaults are generic; they must
# not leak any host or personal path. The SSM parameter has no default — a host
# that has not configured it fails closed (below) rather than guessing.
SSM_PARAMETER="${PENTACLE_AUTH_SSM_PARAMETER:-}"
LOGIN_KEYCHAIN="${PENTACLE_AUTH_LOGIN_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
RAW_CLAUDE="${PENTACLE_AUTH_CLAUDE_BIN:-$HOME/.local/bin/claude}"
AUTH_CONTEXT_MARKER_ROOT="/tmp/pentacle-auth-context"
AUTH_CONTEXT_MARKER_TTL_S=120
AUTH_CONTEXT_MARKER_PATH=""

derive_auth_context_marker_path() {
    stream_id="${AGENT_ORCH_STREAM_ID:-}"
    [ -n "$stream_id" ] || return 1
    digest="$(printf '%s' "$stream_id" | /usr/bin/shasum -a 256)" || return 1
    digest="${digest%% *}"
    [ "${#digest}" -eq 64 ] || return 1
    AUTH_CONTEXT_MARKER_PATH="$AUTH_CONTEXT_MARKER_ROOT/$digest.code"
}

prepare_auth_context_marker() {
    AUTH_CONTEXT_MARKER_PATH=""
    derive_auth_context_marker_path || return 1
    # The reader also clears before every launch attempt. This is defense in
    # depth for direct invocations and retries that do reach this shim.
    /bin/rm -f -- "$AUTH_CONTEXT_MARKER_PATH" >/dev/null 2>&1 || {
        AUTH_CONTEXT_MARKER_PATH=""
        return 1
    }
}

publish_auth_context_marker() {
    [ -n "$AUTH_CONTEXT_MARKER_PATH" ] || return 1
    marker_parent="${AUTH_CONTEXT_MARKER_PATH%/*}"
    (
        umask 077
        temporary=""
        cleanup_temporary() {
            [ -z "$temporary" ] || /bin/rm -f -- "$temporary"
        }
        trap cleanup_temporary EXIT HUP INT TERM
        /bin/mkdir -p -- "$marker_parent" || exit 1
        /bin/chmod 700 "$marker_parent" || exit 1
        temporary="$(/usr/bin/mktemp "$marker_parent/.auth-context.XXXXXX")" || exit 1
        /bin/chmod 600 "$temporary" || exit 1
        printf '%s\n' "$AUTH_CONTEXT_CODE" > "$temporary" || exit 1
        /bin/mv -f -- "$temporary" "$AUTH_CONTEXT_MARKER_PATH" || exit 1
        temporary=""
        trap - EXIT HUP INT TERM
    ) || return 1

    # Keep the marker long enough for the remote failure-path reader, but never
    # leave a daemon-independent pile for a janitor. nohup survives pane exit.
    /usr/bin/nohup /bin/sh -c '
        /bin/sleep "$1"
        /bin/rm -f -- "$2"
    ' _ "$AUTH_CONTEXT_MARKER_TTL_S" "$AUTH_CONTEXT_MARKER_PATH" \
        </dev/null >/dev/null 2>&1 &
}

fail_auth_context() {
    # Keep this stable non-secret marker parseable by SpawnCtl's shared error
    # vocabulary mapping. Do not add command output or credential material.
    publish_auth_context_marker || :
    printf '%s: %s\n' "$AUTH_CONTEXT_CODE" "$1" >&2
    exit 78
}

prepare_auth_context_marker || :
# Fail closed when the host has not configured the credential source, before any
# provider call. No default is guessed and no secret is emitted.
if [ -z "$SSM_PARAMETER" ]; then
    fail_auth_context "ssm_parameter_unconfigured"
fi
credential=""
if ! credential="$(aws ssm get-parameter \
    --name "$SSM_PARAMETER" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)"; then
    fail_auth_context "ssm_fetch_failed"
fi
if [ -z "$credential" ]; then
    fail_auth_context "ssm_fetch_empty"
fi

if ! security unlock-keychain -p "$credential" "$LOGIN_KEYCHAIN" >/dev/null 2>&1; then
    unset credential
    fail_auth_context "keychain_unlock_failed"
fi
unset credential

exec "$RAW_CLAUDE" "$@"
