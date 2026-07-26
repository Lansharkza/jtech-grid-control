# Upgrading

## The short version

1. Extract the new bundle over `/docker/ocpp-cs`, **keeping your `.env`**
2. Container Manager → Project → `ocpp-cs` → **Action → Build**
3. Check `https://<host>:8081/version` reports the new build

That is all. Do not delete the project, the container, or the volume.

## Where the data lives

From v23 it is a plain folder: `/docker/ocpp-cs/data`. You can see it in File
Station, copy it, and it survives deleting or renaming the project, because
Docker does not own it.

```
data/
├── sessions.json    charging history
├── settings.json    tariff, schedule, vehicle profile
└── certs/           TLS certificate
```

Earlier versions used a Docker named volume, which was invisible in File
Station and was removed if you deleted the project with the volume option
ticked. That is why history vanished.

## Recovering history from an old Docker volume

Volume contents live under `/volume1/@docker/`, which File Station hides, so
"the file is not there" often just means you cannot see it. You do not need to.

**1. Check it exists.** Container Manager → **Volume** tab. Look for
`ocpp-cs_jtech-data` or similar. Note the exact name. No such volume means it
was deleted with the project and there is nothing to recover.

**2. Uncomment two blocks in `docker-compose.yml`:**

```yaml
    volumes:
      - ./data:/app/data
      - legacy:/app/legacy:ro          # <- this

volumes:                                # <- and this block
  legacy:
    external: true
    name: ocpp-cs_jtech-data            # <- exact name from the Volume tab
```

**3. Bump the image tag and Build.** On start the container copies
`sessions.json`, `settings.json` and any certificate out of the old volume into
`data/`, and logs:

```
Recovered sessions.json from /app/legacy
Recovered settings.json from /app/legacy
```

It only copies when the destination is empty, so restarting cannot overwrite
newer data.

**4. Comment both blocks out again** and rebuild, so the old volume is no longer
attached. You can then delete it from the Volume tab.

If the container log says nothing about recovering, the volume name is wrong or
the volume is empty.

## Back up first, every time

The dashboard has a **Download backup** button in the Configuration card. It
saves history and settings as a single JSON file. **Restore backup** loads it
again, and can be run twice safely — it merges rather than duplicates.

Do this before any upgrade. It takes five seconds and is the only thing that
makes a mistake survivable.

## Settings survive upgrades now

From v21 all configuration lives in `.env`, which the bundle does not contain —
so extracting over the folder cannot clobber your password or hostnames.

First time only:

1. Copy `env.example` to `.env` (File Station: right-click → Copy → Paste →
   rename to `.env`)
2. Edit `.env` with your password, hostnames and charge point id
3. Delete nothing else

Every upgrade after that: extract and build. `.env` is untouched.

If a release adds a new setting it will appear in `env.example` with a sensible
default, and the compose file falls back to that default when `.env` does not
mention it. Nothing breaks if you never look at it again.

## Step by step

**1. Extract.** Overwrite everything in `/docker/ocpp-cs` except `.env`. Files
that no longer exist in the bundle can be left behind; they are ignored.

**2. Check the tag moved.** Open `docker-compose.yml` and confirm the `image:`
line differs from the running container. Each release bumps it. If it matches,
Container Manager will reuse the old image and nothing will change — this is the
single most common upgrade failure.

**3. Build.** Project → `ocpp-cs` → Action → **Build**. Not Stop/Start, not
Reset. Watch the log to completion.

**4. Verify.**

```
https://<host>:8081/version
```

You want the build number from `VERSION`, `"persistent": true` and, once a
certificate exists, `"tls": true`. The dashboard footer shows the build and
`history saved`.

If the page loads but shows a red version-mismatch bar, the HTML and the Python
are from different releases — extract again and rebuild.

## Rolling back

Old images are kept until you delete them. Container Manager → Image tab lists
`jtech-grid-control:22`, `:23`, `:24` and so on. To go back, set `image:` in
`docker-compose.yml` to the previous tag and Build. The volume is shared across
versions, so history and settings carry over.

Keep the last two or three images and delete the rest — each is around 250 MB.

## Backing up

Everything worth keeping is in the `jtech-data` volume:

- `sessions.json` — charging history
- `settings.json` — tariff, schedule, vehicle profile
- `certs/` — the TLS certificate

On the NAS it lives at `/volume1/@docker/volumes/ocpp-cs_jtech-data/_data/`.
Copy that folder somewhere before any risky change. Restoring is copying it
back.

## What each release touches

| Changed | Rebuild needed |
|---|---|
| `static/*.html` | Yes |
| `central_system.py` | Yes |
| `Dockerfile`, `requirements.txt` | Yes, and the build takes longer |
| `docker-compose.yml` | Yes |
| `.md` files, `check.py`, `verify.sh` | No, documentation and tooling only |

When in doubt, extract everything and build. It costs a few minutes.
