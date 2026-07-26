# Running this on a Synology NAS

Written for DSM 7.2 with Container Manager. On DSM 6 the package is called Docker
and the Project tab is called something else, but the steps are the same shape.

## 1. Put the files on the NAS

Container Manager creates a shared folder called `docker`. Use File Station to
make a folder inside it and upload everything:

```
/volume1/docker/ocpp-cs/
├── Dockerfile
├── docker-compose.yml
├── central_system.py
├── simulator.py
└── static/
    ├── index.html
    └── login.html
```

`static` must be a real subfolder with `index.html` inside it — File Station
won't create it for you if you drag files in loose.

## 2. Pick ports that don't clash

DSM already occupies 5000/5001, and 8080 is commonly taken by Web Station or a
reverse proxy. The compose file publishes:

| Host port | Container | What it's for |
|---|---|---|
| 9000 | 9000 | OCPP WebSocket — the charger connects here |
| 8081 | 8080 | Dashboard and REST API |

If you run Portainer it probably already has 9000. Change the **left-hand** number
in `docker-compose.yml` if either clashes, and remember the charger URL has to
match whatever you pick.

## 3. Create the project

Container Manager → **Project** → **Create**

- Project name: `ocpp-cs`
- Path: `/docker/ocpp-cs`
- Source: it will detect the existing `docker-compose.yml` — accept it
- Click through and let it build. First build pulls the Python base image and
  takes a few minutes.

The container builds from source rather than pulling a published image, so
there's nothing to log into and no registry account needed.

## 4. Open the firewall

If you have DSM's firewall on (Control Panel → Security → Firewall), it will
silently drop the charger's connection. Add an allow rule for TCP 9000 and 8081,
scoped to your LAN subnet rather than all sources.

## 5. Give the NAS a fixed address

The charger stores the URL you type into it and won't follow a DHCP change. Either
set a static IP on the NAS or reserve its lease on your router.

## 6. Point the charger at it

In the Teltonika app, OCPP settings:

- URL: `ws://<nas-ip>:9000/` — the trailing slash is required by Teltonika
- Charge point ID: the charger's serial number
- Enable the OCPP slider, save

Then open `http://<nas-ip>:8081/`, sign in, and watch for `Connected` followed by
`BootNotification` in the message log.

Set `OCPP_ADMIN_PASSWORD` in `docker-compose.yml` before you do anything else. If
you leave it unset, the container generates one per start and prints it:

```bash
sudo docker logs ocpp-cs | head -20
```

## 7. Check it before the charger is involved

You can exercise the whole stack from the NAS itself without touching the
charger. In Container Manager, open the container → **Terminal** → create a bash
session:

```bash
python simulator.py --url ws://127.0.0.1:9000 --id TEST01
```

A fake charger appears in the dashboard. Ctrl-C when you're done.

## Operating notes

- **Logs**: Container Manager → Container → `ocpp-cs` → Log. Rotation is capped at
  3 × 10 MB in the compose file so it can't fill your volume.
- **Auto-start**: `restart: unless-stopped` brings it back after a NAS reboot or a
  crash. It won't restart if you stopped it deliberately.
- **Updating the code**: replace the files in the shared folder, then Project →
  **Build** again. The container restarts with the new code.
- **State is in memory.** A rebuild or reboot wipes transaction history and the
  event log. Live charger state repopulates within a heartbeat.
- **Timezone**: the compose file sets `TZ: Africa/Johannesburg` so log timestamps
  match DSM. OCPP itself always uses UTC on the wire, which is correct.

## Optional: TLS via DSM's reverse proxy

If you want `wss://` — needed if you raise the charger's SecurityProfile to 2 or
3, or if this is ever reachable from outside the LAN:

Control Panel → Login Portal → Advanced → **Reverse Proxy** → Create

- Source: HTTPS, your hostname, port 443
- Destination: HTTP, `localhost`, port 9000
- On the **Custom Header** tab, click Create → **WebSocket**. Without this the
  upgrade fails and the charger just retries forever.

Then set the charger URL to `wss://<hostname>/` and let DSM's Let's Encrypt
certificate handle the cert. Do the same on a second rule for the dashboard if
you want that over HTTPS too.

One caveat: the charger validates certificates. A self-signed cert won't work —
use a real one from DSM's Let's Encrypt integration.

## If the build fails

**`stat <filename>: file does not exist`** — Docker can't see that file in the
build context. Three causes, in order of likelihood:

1. **The project path isn't the folder with the files in it.** Container Manager
   builds relative to the project path, and `build: .` means "this folder". Check
   Project → Settings and confirm it points at `/docker/ocpp-cs`, not its parent.
2. **The file didn't upload, or uploaded under a different name.** Windows hides
   known extensions, so a file you saved as `requirements.txt` in Notepad is often
   really `requirements.txt.txt`. In File Station, turn on Settings → show file
   extensions and look at the actual names.
3. **Case.** `Static/` is not `static/`. The container filesystem is
   case-sensitive even though DSM's shared folders usually aren't.

Verify what Docker can actually see by SSHing in and listing the folder:

```bash
ls -la /volume1/docker/ocpp-cs /volume1/docker/ocpp-cs/static
```

## If the charger won't connect

Work down this list:

1. Container running and healthy in Container Manager?
2. `curl http://<nas-ip>:8081/api/chargers` from a laptop on the same LAN —
   proves the container is reachable off-box, not just from the NAS.
3. DSM firewall rule present?
4. Charger URL uses `ws://` (not `http://`) and ends with `/`?
5. Charger actually online — check its own status screen in the Teltonika app.
6. Container log: a client connecting without the `ocpp1.6` subprotocol gets
   logged and closed, which tells you it reached the server but negotiated wrong.
