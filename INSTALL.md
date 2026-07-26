# Install / reinstall

Everything needed is in this folder. Nothing is fetched from a private registry.

## Synology (Container Manager)

1. **Copy the folder** to the NAS so you have:

   ```
   /volume1/docker/ocpp-cs/
   ├── Dockerfile
   ├── docker-compose.yml
   ├── central_system.py
   ├── simulator.py
   ├── requirements.txt
   └── static/
       ├── index.html
       ├── login.html
       └── favicon.svg
   ```

   `static` must contain the three files directly.

2. **Edit `docker-compose.yml`** and set `OCPP_ADMIN_PASSWORD`. Leave
   `OCPP_AUTHORIZATION_KEY` commented out unless the charger's SecurityProfile
   is 1 or 2 — enabling it against a SecurityProfile 0 charger locks it out.

3. **Container Manager → Project → Create.** Name `ocpp-cs`, path
   `/docker/ocpp-cs`. It detects `docker-compose.yml`. Build.

4. **Firewall**: allow TCP 8081 and 9081 from the LAN
   (Control Panel → Security → Firewall).

5. **Verify**: open `http://<nas-ip>:8081/version`. It must report the build in
   `VERSION`. Anything else means the image did not rebuild.

## Anywhere else

```bash
pip install -r requirements.txt
OCPP_ADMIN_PASSWORD=something-long python central_system.py
```

Dashboard on 8080, OCPP on 9000. Override with `--api-port` / `--ocpp-port`.

## Point the charger at it

Teltonika app over Bluetooth → OCPP settings:

- URL `ws://<nas-ip>:9081/` — trailing slash required
- Charge point ID: the serial number
- Enable OCPP, save

Then sign in and press **Apply recommended** in the Configuration card.

## Rebuilding after a change

Container Manager reuses an image if the tag already exists, so **bump the tag**
in `docker-compose.yml` every time:

```yaml
image: jtech-grid-control:9
```

Then Action → Build. Confirm via `/version`. This is the single most common
cause of "my changes did nothing".

## Persistence

Tariff, schedule and session history live in `/app/data`. The compose file uses
a **named volume** (`jtech-data`) rather than a `./data` bind mount, because on
Synology a bind-mounted folder is created root-owned and the container — which
runs as an unprivileged user — cannot write to it. That failure is silent.

The named volume survives rebuilds, container deletion and NAS reboots. It is
removed only by `docker compose down -v`, or by deleting it in Container
Manager's Volume tab.

Check it is working: the dashboard footer should read `history saved`, and
`/version` should report `"persistent": true`.

To back the data up, Container Manager → Container → export, or copy
`/volume1/@docker/volumes/ocpp-cs_jtech-data/_data/`.

## After editing the pages

Run the static check before rebuilding:

```bash
python check.py
```

It catches the failures that a browser hides: inline styles and event handlers
that CSP silently drops, unterminated tags that swallow the stylesheet, script
references to elements that do not exist, and `data-act` buttons with no
handler. All of these load without error and simply do nothing.

## Verifying an install

```bash
./verify.sh http://192.168.0.121:8081
```

Checks the build number, that the login page and stylesheet are served, and that
the API refuses unauthenticated requests.

## Things that will bite you

| Symptom | Cause |
|---|---|
| Changes have no effect | Image tag not bumped, or project path is not the folder you uploaded to |
| Login accepted, bounces back to login | `OCPP_COOKIE_SECURE=1` without HTTPS — browsers drop a Secure cookie. From v21 the server detects this, warns, and falls back so sign-in still works |
| Charger stops connecting | `OCPP_AUTHORIZATION_KEY` set while charger is at SecurityProfile 0, or the OCPP port changed and the charger URL wasn't updated |
| Page renders as unstyled text | Broken tag in the `<head>` swallowing the stylesheet |
| Dashboard looks frozen | `MeterValueSampleInterval` still at the 3600 factory default |
| `stat <file>: file does not exist` | File missing from the build context, or a hidden `.txt` extension |
| Schedule fires at the wrong hour | Container running UTC because `tzdata` is missing — check the timezone shown by the schedule card |
| Auto-start buttons do nothing | Fixed in v12; earlier builds disabled them along with the solar mode buttons |
| Buttons do nothing, no toast appears | An inline `onclick` was added — CSP blocks inline handlers. Wire it through `ACTIONS` instead |
| Colours missing from cards, tiles or buttons | An inline `style="..."` was added — CSP blocks inline styles. Use a CSS class instead |
| 7-day history empties on restart | Data directory not writable. The footer says "history not saved" and `/version` reports `"persistent": false`. Use the named volume, not a `./data` bind mount |
