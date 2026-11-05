#!/bin/sh
# Runs on every process launch, including Docker's automatic restarts.
set -eu
export LC_ALL=C
umask 077

fail() {
    printf '%s\n' 'GGUF server preflight failed' >&2
    exit 2
}

bounded_digest() {
    path=$1
    declared_size=$2
    maximum=$3
    case "$declared_size" in ''|0*|*[!0-9]*) fail ;; esac
    [ "${#declared_size}" -le 13 ] && [ "$declared_size" -le "$maximum" ] || fail
    [ -f "$path" ] && [ ! -L "$path" ] || fail
    before=$(stat -c '%d:%i:%s:%y:%z' "$path") || fail
    size=$(stat -c %s "$path") || fail
    [ "$size" = "$declared_size" ] || fail
    # Reading at most the expected size + 1 detects growth without following it.
    result=$(head -c "$((declared_size + 1))" "$path" | sha256sum) || fail
    after=$(stat -c '%d:%i:%s:%y:%z' "$path") || fail
    [ "$before" = "$after" ] || fail
    printf '%s' "${result%% *}"
}

# Stage a bounded private snapshot so the server uses exactly the validated keys.
key_source=/run/secrets/gguf_api_keys
key_snapshot=/tmp/gguf-api-keys
[ -f "$key_source" ] && [ ! -L "$key_source" ] && [ -r "$key_source" ] || fail
head -c 16385 "$key_source" > "$key_snapshot" || fail
key_size=$(stat -c %s "$key_snapshot") || fail
[ "$key_size" -ge 1 ] && [ "$key_size" -le 16384 ] || fail
awk '
    /^#/ || /^$/ { next }
    length($0) < 32 || length($0) > 512 || /[^!-~]/ { bad = 1 }
    { if (seen[$0]++) bad = 1; count++ }
    END { if (bad || count < 1 || count > 4) exit 1 }
' "$key_snapshot" || fail

# Receipts can only be written by the independent verifier, not this service.
[ -f /models/model.gguf ] && [ ! -L /models/model.gguf ] || fail
[ "$(cat /verification/model.alias)" = "${GGUF_MODEL_ALIAS:?}" ] || fail
[ "$(cat /verification/context-size)" = "${GGUF_CONTEXT_SIZE:?}" ] || fail
expected=$(cat /verification/model.sha256) || fail
[ "${#expected}" -eq 64 ] || fail
case "$expected" in *[!0-9a-f]*) fail ;; esac
model_size=$(cat /verification/model.size) || fail
actual=$(bounded_digest /models/model.gguf "$model_size" 1099511627776) || fail
[ "$actual" = "$expected" ] || fail
expected_manifest=$(cat /verification/manifest.sha256) || fail
[ "${#expected_manifest}" -eq 64 ] || fail
case "$expected_manifest" in *[!0-9a-f]*) fail ;; esac
manifest_size=$(cat /verification/manifest.size) || fail
actual_manifest=$(bounded_digest /models/manifest.json "$manifest_size" 65536) || fail
[ "$actual_manifest" = "$expected_manifest" ] || fail

exec /app/llama-server "$@"
