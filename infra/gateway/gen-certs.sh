#!/bin/sh
# Generate the gateway's self-signed TLS certificate for
# local development (https://localhost:8443).
#
#   sh infra/gateway/gen-certs.sh             # create if missing
#   sh infra/gateway/gen-certs.sh --force     # always regenerate
#   sh infra/gateway/gen-certs.sh --out DIR   # write somewhere else
#
# Output: <dir>/server.crt and <dir>/server.key (default dir:
# infra/gateway/certs, mounted read-only into the gateway container at
# /etc/nginx/certs). Valid 825 days, SAN DNS:localhost and IP:127.0.0.1.
# Idempotent: if both files already exist nothing is done unless
# --force is given. Run by `make certs` and automatically by `make up`.
#
# Uses an OpenSSL config file instead of `-addext` so it also works with
# the LibreSSL that ships as /usr/bin/openssl on macOS.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT_DIR="$SCRIPT_DIR/certs"
FORCE=0
DAYS=825

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --out)
            [ $# -ge 2 ] || { echo "gen-certs: --out needs a directory" >&2; exit 2; }
            OUT_DIR=$2
            shift
            ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "gen-certs: unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift
done

CRT="$OUT_DIR/server.crt"
KEY="$OUT_DIR/server.key"

if [ "$FORCE" -eq 0 ] && [ -s "$CRT" ] && [ -s "$KEY" ]; then
    echo "gen-certs: $CRT already exists (use --force to regenerate)"
    exit 0
fi

command -v openssl >/dev/null 2>&1 || {
    echo "gen-certs: openssl is required but was not found in PATH" >&2
    exit 1
}

mkdir -p "$OUT_DIR"
TMP_CONF=$(mktemp "${TMPDIR:-/tmp}/alphamind-cert.XXXXXX")
trap 'rm -f "$TMP_CONF"' EXIT INT TERM

cat > "$TMP_CONF" <<'EOF'
[req]
prompt = no
distinguished_name = dn
x509_extensions = v3_ext

[dn]
CN = localhost
O = AlphaMind development

[v3_ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
subjectKeyIdentifier = hash
EOF

# Write to temporary names first so a failed run never leaves a
# half-written pair that the idempotency check would then accept.
umask 077
openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days "$DAYS" \
    -config "$TMP_CONF" \
    -keyout "$KEY.tmp" -out "$CRT.tmp" >/dev/null 2>&1 || {
    rm -f "$KEY.tmp" "$CRT.tmp"
    echo "gen-certs: openssl failed" >&2
    exit 1
}
mv -f "$KEY.tmp" "$KEY"
mv -f "$CRT.tmp" "$CRT"
# The certificate is public; the key stays owner-only (nginx's master
# process reads it as root before dropping privileges).
chmod 644 "$CRT"
chmod 600 "$KEY"

echo "gen-certs: wrote $CRT and $KEY (self-signed, $DAYS days, localhost/127.0.0.1)"
