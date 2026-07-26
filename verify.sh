#!/bin/sh
# Quick post-install check. Usage: ./verify.sh http://192.168.1.50:8081
BASE="${1:-http://localhost:8081}"
echo "Checking $BASE"
printf '  /version    : '; curl -fsS "$BASE/version" || echo "FAILED - old build or not running"
echo
printf '  /login      : '; curl -fsS -o /dev/null -w '%{http_code}\n' "$BASE/login"
printf '  /favicon    : '; curl -fsS -o /dev/null -w '%{http_code}\n' "$BASE/favicon.svg"
printf '  auth guard  : '; curl -fsS -o /dev/null -w '%{http_code} (expect 401)\n' "$BASE/api/chargers"
printf '  stylesheet  : '; curl -fsS "$BASE/login" | grep -c ':root{' | sed 's/^1$/present/;s/^0$/MISSING/'
