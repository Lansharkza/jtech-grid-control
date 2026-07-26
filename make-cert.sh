#!/bin/sh
# Generate a self-signed certificate for the dashboard.
#
#   ./make-cert.sh 192.168.1.50
#
# Pass every address you will use to reach it — the IP, and any hostname.
# iOS and modern browsers ignore the Common Name and require the address to
# appear in subjectAltName, which is what this sets up.

set -e
DIR="$(dirname "$0")/certs"
mkdir -p "$DIR"

if [ $# -eq 0 ]; then
  echo "Usage: $0 <ip-or-hostname> [more...]"
  echo "Example: $0 192.168.1.50 nas.local"
  exit 1
fi

ALT=""
N=1
for HOST in "$@"; do
  case "$HOST" in
    *[0-9].[0-9]*[0-9]) ALT="$ALT
IP.$N = $HOST" ;;
    *) ALT="$ALT
DNS.$N = $HOST" ;;
  esac
  N=$((N + 1))
done

cat > "$DIR/openssl.cnf" <<CONF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no

[dn]
CN = $1
O = JTech Grid Control

[v3]
subjectAltName = @alt
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt]$ALT
CONF

openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
  -keyout "$DIR/server.key" -out "$DIR/server.crt" \
  -config "$DIR/openssl.cnf"

chmod 600 "$DIR/server.key"
echo
echo "Written to $DIR/"
openssl x509 -in "$DIR/server.crt" -noout -subject -dates \
  -ext subjectAltName | sed 's/^/  /'
echo
echo "Next: set OCPP_COOKIE_SECURE=1 in docker-compose.yml, bump the image tag,"
echo "and rebuild. Then install $DIR/server.crt on each device that will"
echo "connect — see IPHONE.md for the iOS steps."
