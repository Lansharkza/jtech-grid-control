# Publishing the dashboard remotely

Port 443 is taken by other containers, so TLS terminates inside this container
on 8081. That encrypts the password and everything else on the wire, which is
the point.

## Setup

**Nothing to generate or copy.** On first start the container creates a
self-signed certificate covering every name in `OCPP_TRUSTED_ORIGINS`, stores it
on the data volume, and reuses it on every restart.

**1. Configure.** Already set in `docker-compose.yml`:

```yaml
OCPP_COOKIE_SECURE: "1"
OCPP_AUTO_CERT: "1"
OCPP_TRUST_PROXY: "0"
OCPP_TRUSTED_ORIGINS: "hosts,fqdn"
```

Every address you will type into a browser must appear in
`OCPP_TRUSTED_ORIGINS`, because that list is what the certificate covers.

**2. Rebuild** with a bumped image tag. The startup log should read:

```
Generated a self-signed certificate for ev.moeken.co.za, ...
Dashboard  https://0.0.0.0:8080/
```

**3. Install the certificate** on each device: browse to
`https://<host>:8081/certificate` and open the downloaded file. iOS needs the
extra trust step in IPHONE.md.

`make-cert.sh` is still there if you would rather generate one yourself; point
`OCPP_TLS_CERT` and `OCPP_TLS_KEY` at it and set `OCPP_AUTO_CERT=0`.

**4. Router.** Forward external **8081 to 8081** on the NAS. Nothing else.
**Never forward 9081** — see below.

**5. Harden DSM.** Control Panel → Security → Account: enable auto-block after
failed logins. Firewall: allow 8081 from anywhere, everything else LAN-only.

## What self-signed does and does not give you

**Does**: full encryption. Your password, session cookie, tariff and charger
configuration are ciphertext on the wire. Anyone tapping the link sees nothing
useful. This is the same TLS a public site uses.

**Does not**: prove the server is yours. A browser trusts a public certificate
because a certificate authority vouched for it; nobody vouches for this one. An
attacker positioned to intercept traffic could present their own certificate,
and you would see the same warning you already see and might click through.

On a LAN that gap is theoretical. Over the internet it is smaller than it
sounds but not zero — so **install the certificate on the devices you use** (see
IPHONE.md). Once installed, the browser trusts that exact certificate, the
warning disappears, and a substituted certificate would then fail loudly. That
turns the weakness into a strength: your devices accept one certificate and
nothing else.

## Two consequences to expect

**Every new device warns once.** Click through, or install the certificate.
Safari on iOS will not add the app to the home screen until the certificate is
installed *and* trusted — both steps, see IPHONE.md.

**`http://` on 8081 no longer works.** The port speaks TLS now, so a plain HTTP
request gets a protocol error rather than a redirect. Always type `https://`.

## A trusted certificate without port 443

If the warnings become tiresome, a Let's Encrypt certificate can be issued using
a **DNS-01 challenge**, which proves control of `moeken.co.za` by writing a TXT
record instead of serving a file on port 80. Tools like `acme.sh` automate this
against most DNS providers' APIs. Drop the resulting `fullchain.pem` and
`privkey.pem` into `certs/` as `server.crt` and `server.key`, and set a reminder
to renew every 60 days. No port 80, no port 443, no browser warnings.

## Never expose the OCPP port

Port 9081 is the charger's WebSocket endpoint. At `SecurityProfile 0` it is
unauthenticated — anyone who reaches it can register as a charge point, feed
false meter readings, and pollute your history. It has no reason to leave the
LAN, since the charger and the NAS are on the same network.

Set `OCPP_ALLOWED_CHARGE_POINTS` to your charger's id regardless.

## Once it is live

Check `https://ev.moeken.co.za/version`. You want the current build,
`"persistent": true`, and a padlock with no warning. Then sign in and confirm a
control action works — if the schedule or mode buttons return an error, the
`OCPP_TRUSTED_ORIGINS` value does not match the hostname in the address bar.
