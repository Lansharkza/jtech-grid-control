# Security

What was audited, what was fixed, and what you must still do before this faces
the internet.

## SQL injection

Not applicable. There is no database and no SQL anywhere in the codebase. State
lives in Python dictionaries and two JSON files (`settings.json`,
`sessions.json`) written with `json.dump`, which cannot be escaped out of. The
only user-controlled value that reaches a filename is none — both paths are
constants.

## Cross-site scripting

**This was a real finding and is now fixed.**

The dashboard renders data that a charger controls: status strings, error codes,
configuration keys and values, RFID tags, vendor `DataTransfer` payloads, and
the charge point id itself. All of it was being interpolated into `innerHTML`
unescaped. Anyone who could reach the OCPP port — which is unauthenticated by
default — could have stored a payload that executed in the operator's browser
and used their session to control the charger.

Fixed by escaping every untrusted value at the point it enters the DOM. Verified
by connecting a hostile client and sending schema-legal payloads
(`<script>document.title='PWNED'</script>`, `<img src=x onerror=alert(1)>`,
`<svg onload=x>` as an idTag): zero `alert()` calls, zero injected elements,
payloads rendered as inert text.

Two further layers:

- **Charge point ids** must match `^[A-Za-z0-9._:-]{1,48}$`. Ids containing
  markup, path traversal, or excessive length are refused at connection and
  logged. Verified.
- **Content-Security-Policy** pins SHA-256 hashes of the inline `<script>` and
  `<style>` blocks. `default-src 'none'` with no `unsafe-inline`, so injected
  script cannot run even if an escape were missed, and no external resource can
  be loaded. `frame-ancestors 'none'` blocks clickjacking.

Because the policy has no `'unsafe-inline'`, inline `onclick`/`onchange`
attributes are also blocked — they are inline script as far as CSP is concerned.
All controls are therefore wired up with delegated `addEventListener` calls
inside the hashed script block. Adding an inline handler to the markup would
silently stop working.

If a CSP hash ever mismatches, the dashboard would render unstyled and inert.
Set `OCPP_STRICT_CSP=0` to fall back to a permissive policy while you
investigate.

## Authentication

| Control | Implementation |
|---|---|
| Session token | `secrets.token_urlsafe(32)`, 256 bits from the OS CSPRNG |
| Cookie | `HttpOnly`, `SameSite=Lax`, `Secure` when `OCPP_COOKIE_SECURE=1` |
| Password comparison | `hmac.compare_digest`, constant time |
| Brute force | 5 failures per IP per 5 minutes, then 429 |
| Session lifetime | 8 hours, expired tokens purged on each login |
| Logout | Token deleted server-side, not just the cookie |

Every API route and both pages require a valid session. The only unauthenticated
endpoints are `/login`, `/api/auth/login`, `/favicon.svg` and `/version`.

**CSRF**: state-changing requests with a cross-origin `Origin` header are
rejected with 403, on top of `SameSite=Lax`. Verified.

## Input validation

All request bodies are Pydantic models with explicit bounds — idTag ≤ 20 chars,
limit 0–80 A, connector 0–8, config key ≤ 50 chars, value ≤ 500, schedule hours
0–23. Out-of-range input returns 422 before any OCPP message is sent. Verified.

Inbound OCPP messages are validated against the official OCPP 1.6 JSON schemas
by the `ocpp` library, which rejects oversized and malformed payloads before
they reach application code.

## Response headers

```
Content-Security-Policy: default-src 'none'; script-src 'sha256-...'; ...
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Permissions-Policy: geolocation=(), microphone=(), camera=()
Strict-Transport-Security: max-age=31536000  (when OCPP_COOKIE_SECURE=1)
```

## What is still NOT safe to expose

**Do not port-forward 9081 (the OCPP port).** With `SecurityProfile 0` it is
unauthenticated: anyone who can reach it can connect as a charge point, feed
false meter readings, and occupy your charge point id. Mitigations, in order of
preference:

1. Keep it on the LAN. The charger and the NAS are on the same network — the
   OCPP port never needs to leave it.
2. Set `OCPP_ALLOWED_CHARGE_POINTS=EVC121` so only your serial is accepted.
3. Raise the charger to `SecurityProfile 1` and set `OCPP_AUTHORIZATION_KEY` to
   the same value, which enforces HTTP Basic auth on the WebSocket. Do this in
   the order documented in the README or you will lock the charger out.

**`ACCEPT_UNKNOWN_TAGS = True`** means any RFID card starts a charge. That is a
physical-access issue, not a network one, but worth knowing.

**Sessions are in memory.** A restart signs everyone out. There is no
multi-user support, no audit trail of who did what, and no password rotation.

## Before you expose the dashboard

1. **TLS only.** Terminate HTTPS at DSM's reverse proxy with a Let's Encrypt
   certificate. Forward only 8081. Never expose plain HTTP — the session cookie
   would travel in the clear.
2. **Set `OCPP_COOKIE_SECURE=1`** once HTTPS is in front, so the cookie is never
   sent unencrypted. It breaks sign-in over plain HTTP, which is the correct
   failure mode.
3. **Use a long passphrase.** The rate limiter slows guessing but does not stop
   a weak password. Four or five unrelated words beats symbol substitution.
4. **Set `OCPP_ALLOWED_CHARGE_POINTS`** to your charger's id.
5. **Prefer a VPN.** Tailscale or DSM's VPN Server gives you remote access with
   no public attack surface at all. For a single-operator home charger this is
   a better trade than publishing a login page, and it takes about ten minutes.

## Residual risk

- The rate limiter keys on `request.client.host`. Behind a reverse proxy that is
  the proxy's address, so all clients share one bucket — a single attacker could
  lock you out. If you front this with a proxy, rate-limit there instead.
- No account lockout, 2FA, or password rotation.
- The admin password sits in plain text in `docker-compose.yml`. Anyone with
  file access to the NAS can read it.
- Dependencies are pinned but not automatically patched. Check for `fastapi`,
  `uvicorn` and `websockets` advisories periodically.
