#!/usr/bin/env python3
"""Container healthcheck. Works whether the server is on HTTP or HTTPS, and
uses the unauthenticated /version endpoint. Exits 0 if healthy, 1 otherwise."""
import ssl
import sys
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

for scheme in ("https", "http"):
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:8080/version",
                                    timeout=3, context=ctx) as r:
            if r.status == 200:
                sys.exit(0)
    except Exception:
        continue

sys.exit(1)
