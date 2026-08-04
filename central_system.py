"""
JTech Grid Control — an OCPP 1.6J central system for the Teltonika TeltoCharge EVC121.

Runs two servers in one asyncio process:
  * a WebSocket server (subprotocol "ocpp1.6") the charger connects to
  * a FastAPI REST API + operator dashboard you drive it from

Usage:
    python central_system.py
Then point the charger's OCPP URL at  ws://<this-host>:9000/<ChargePointId>
and open  http://<this-host>:8080/
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import uvicorn
import websockets
from urllib.parse import unquote
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger("cs")


# --------------------------------------------------------------------------
# Version shim: the `ocpp` library dropped the "Payload" class-name suffix in
# 2.0. Resolving by name keeps this file working on either generation.
# --------------------------------------------------------------------------
def _resolve(module, name: str):
    for candidate in (name, name + "Payload"):
        if hasattr(module, candidate):
            return getattr(module, candidate)
    raise AttributeError(f"{module.__name__} has no {name}")


def result(name: str, **kwargs):
    return _resolve(call_result, name)(**kwargs)


def request(name: str, **kwargs):
    return _resolve(call, name)(**kwargs)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()



# --------------------------------------------------------------------------
# Settings file. A plain, visible file in the data folder — File Station cannot
# create names beginning with a dot, so .env is awkward on a NAS. Values here
# win over the environment, and a commented template is written on first start.
# --------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("OCPP_CONFIG_FILE", "/app/data/config.env")

CONFIG_TEMPLATE = """# JTech Grid Control settings
#
# Edit in File Station: right-click -> Open with Text Editor. Save, then
# restart the container in Container Manager. Upgrades never overwrite this
# file, and blank or missing entries fall back to sensible defaults.
#
# Ports are NOT set here - change those in docker-compose.yml.

# --- sign-in ---------------------------------------------------------------
OCPP_ADMIN_USER=admin
# Leave blank and a password is generated and printed to the container log on
# every start. Set a long passphrase before exposing this to the internet.
OCPP_ADMIN_PASSWORD=

# --- https -----------------------------------------------------------------
# Every hostname and IP you will type into a browser. The self-signed
# certificate is generated to cover exactly this list. After changing it,
# delete the data/certs folder so a new certificate is issued.
OCPP_TRUSTED_ORIGINS=

# --- charger ---------------------------------------------------------------
# Only this charge point id may connect. Recommended.
OCPP_ALLOWED_CHARGE_POINTS=

# --- initial defaults ------------------------------------------------------
# Once changed from the dashboard these live in settings.json and this is
# ignored.
OCPP_PRICE_PER_KWH=3.55
OCPP_CURRENCY=R
OCPP_BATTERY_KWH=60
OCPP_TARGET_SOC=80

# --- Solar Assistant (optional) --------------------------------------------
# Link an inverter managed by Solar Assistant so its work mode follows the
# charger. While the car is actively charging the mode is set to
# SOLAR_ASSISTANT_MODE_CHARGING; the rest of the time it returns to
# SOLAR_ASSISTANT_MODE_IDLE. Leave the host blank to disable entirely.
#
# The password is your Solar Assistant *local* device password. It never leaves
# this file. Set a local password in Solar Assistant first if you have not.
SOLAR_ASSISTANT_HOST=
SOLAR_ASSISTANT_USER=admin
SOLAR_ASSISTANT_PASSWORD=
# Exact work-mode strings from the Solar Assistant dropdown. Defaults suit a
# Sunsynk/Deye: keep the battery on the essential load while charging, share to
# the whole board (CT) otherwise.
SOLAR_ASSISTANT_MODE_CHARGING=Zero export to load
SOLAR_ASSISTANT_MODE_IDLE=Zero export to CT

TZ=Africa/Johannesburg
"""


def load_config_file() -> None:
    """Apply KEY=VALUE lines from the settings file, creating it if absent."""
    if not os.path.isfile(CONFIG_PATH):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as handle:
                handle.write(CONFIG_TEMPLATE)
            print(f"Created {CONFIG_PATH} — edit it in File Station, then restart")
        except OSError:
            pass                      # not writable yet; defaults still apply
        return

    applied = []
    try:
        with open(CONFIG_PATH) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if not key or not value:
                    continue
                os.environ[key] = value
                applied.append(key)
    except OSError:
        return

    if "TZ" in applied:
        try:
            time.tzset()
        except AttributeError:
            pass
    if applied:
        print(f"Loaded {len(applied)} settings from {CONFIG_PATH}")


load_config_file()

# --------------------------------------------------------------------------
# In-memory state
# --------------------------------------------------------------------------
CHARGERS: Dict[str, "ChargePoint"] = {}
# Deliberate "take offline" intent, kept by charger id so it persists across the
# charger reconnecting. Prevents the auto-operative-on-reconnect from overriding
# an operator's explicit decision to keep the charger offline.
OFFLINE_INTENT: Dict[str, bool] = {}
EVENTS: deque = deque(maxlen=500)
_txn_counter = itertools.count(1)

# Any tag in here is accepted by Authorize / StartTransaction. Swap for a DB
# lookup when you want real access control.
ALLOWED_TAGS = {"DEMO", "FREEVEND"}
ACCEPT_UNKNOWN_TAGS = True

# When the car reaches target SOC it stops drawing and the charger reports
# SuspendedEV while keeping the transaction open. Record the session at that
# moment rather than at the eventual StopTransaction, so a scheduled window that
# outlasts the actual charge is not logged as hours of charging. Set to 0 to
# revert to recording only at StopTransaction.
FINALISE_ON_SUSPENDED_EV = os.environ.get("OCPP_FINALISE_ON_SUSPENDED_EV", "1") != "0"

# Set this to the charger's AuthorizationKey (32-40 chars) if you raise its
# SecurityProfile to 1 or 2. Empty string = no WebSocket auth (SecurityProfile 0).
AUTHORIZATION_KEY = os.environ.get("OCPP_AUTHORIZATION_KEY", "")

# The EVC121 accepts 60-86400 for HeartbeatInterval; anything lower is rejected.
HEARTBEAT_INTERVAL = 60


# --------------------------------------------------------------------------
# Solar Assistant integration. Optional. When a Solar Assistant host is set,
# the inverter's work mode is driven to follow the charger: "charging" mode
# while a session is active, "idle" mode otherwise. Talks to Solar Assistant's
# local REST API (https://solar-assistant.io/help/integration/rest-api) using
# the built-in urllib in a thread, so no extra dependency is pulled in.
# --------------------------------------------------------------------------
SA_HOST = os.environ.get("SOLAR_ASSISTANT_HOST", "").strip()
SA_USER = os.environ.get("SOLAR_ASSISTANT_USER", "admin").strip()
SA_PASSWORD = os.environ.get("SOLAR_ASSISTANT_PASSWORD", "")
SA_MODE_CHARGING = os.environ.get("SOLAR_ASSISTANT_MODE_CHARGING", "Zero export to load").strip()
SA_MODE_IDLE = os.environ.get("SOLAR_ASSISTANT_MODE_IDLE", "Zero export to CT").strip()
# The work modes offered on the dashboard, in the exact strings the inverter
# accepts. "Selling first" is deliberately excluded — battery-sharing modes only.
SA_WORK_MODES = ["Zero export to load", "Zero export to CT"]
SA_WORK_MODE_TOPIC = "inverter_1/work_mode"
SA_ENABLED = bool(SA_HOST)

# Whether the automatic follow-the-charger behaviour is on. Toggled from the
# dashboard; the host being configured is what enables the feature at all.
SA_AUTO = {"enabled": SA_ENABLED}

# When the user last set a work mode by hand. The solar watcher leaves the
# inverter alone for a grace period after, so a manual choice isn't immediately
# overridden by the automation.
SA_MANUAL_OVERRIDE_AT = [0.0]
SA_MANUAL_GRACE_SECONDS = 120


class SolarAssistant:
    """Minimal client for Solar Assistant's local REST API. All network calls
    run in a thread so they never block the asyncio loop, and every call is
    best-effort: a failure is logged and swallowed so the charger is never held
    up by the inverter link."""

    def __init__(self, host: str, user: str, password: str):
        self.base = f"http://{host}".rstrip("/")
        self.user = user
        self.password = password
        self._auth = None
        if user and password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._auth = f"Basic {token}"

    def _blocking_get(self, topic: Optional[str] = None) -> Any:
        url = f"{self.base}/api/v1/metrics"
        if topic:
            url += "?topic=" + urllib.parse.quote(topic, safe="/*")
        req = urllib.request.Request(url)
        if self._auth:
            req.add_header("Authorization", self._auth)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def _blocking_set(self, topic: str, value: str) -> Dict[str, Any]:
        url = f"{self.base}/api/v1/metrics"
        body = json.dumps({"topic": topic, "value": value}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self._auth:
            req.add_header("Authorization", self._auth)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    async def get_metrics(self, topic: Optional[str] = None) -> Optional[Any]:
        try:
            return await asyncio.to_thread(self._blocking_get, topic)
        except Exception as exc:  # noqa: BLE001 - best effort by design
            log.warning("solar assistant read failed: %s", exc)
            return None

    async def get_work_mode(self) -> Optional[str]:
        data = await self.get_metrics(SA_WORK_MODE_TOPIC)
        if isinstance(data, list):
            for entry in data:
                if entry.get("topic") == SA_WORK_MODE_TOPIC:
                    return entry.get("value")
        return None

    async def set_work_mode(self, value: str) -> bool:
        """Set the inverter work mode, but only if it is not already there, so
        we never thrash the inverter with redundant writes. Returns True on a
        confirmed change, False otherwise (including 'already set')."""
        current = await self.get_work_mode()
        if current == value:
            return False
        try:
            result = await asyncio.to_thread(self._blocking_set, SA_WORK_MODE_TOPIC, value)
        except Exception as exc:  # noqa: BLE001
            log.warning("solar assistant set work mode failed: %s", exc)
            return False
        ok = isinstance(result, dict) and result.get("result") == "ok"
        if ok:
            log.info("solar assistant: work mode -> %s", value)
        else:
            log.warning("solar assistant rejected work mode %r: %s", value, result)
        return ok

    async def status(self) -> Optional[Dict[str, Any]]:
        """A compact snapshot for the dashboard panel."""
        data = await self.get_metrics()
        if not isinstance(data, list):
            return None
        wanted = {
            "inverter_1/work_mode": "workMode",
            "inverter_1/battery_state_of_charge": "batterySoc",
            "inverter_1/battery_power": "batteryW",
            "inverter_1/pv_power": "pvW",
            "inverter_1/load_power": "loadW",
            "inverter_1/grid_power": "gridW",
            "inverter_1/battery_temperature": "batteryTemp",
        }
        out: Dict[str, Any] = {}
        for entry in data:
            key = wanted.get(entry.get("topic"))
            if key:
                out[key] = entry.get("value")
        return out or None


SA_CLIENT: Optional[SolarAssistant] = (
    SolarAssistant(SA_HOST, SA_USER, SA_PASSWORD) if SA_ENABLED else None
)


async def _choose_charging_mode() -> str:
    """While a car is charging, decide the inverter work mode from live solar:
    share to the whole board (idle mode) when there is enough sun AND the
    battery is above the floor, so the car soaks up surplus; otherwise keep the
    battery on the essential load. Falls back to the protective 'load' mode if
    the inverter can't be read."""
    snapshot = await SA_CLIENT.status() if SA_CLIENT else None
    if not snapshot:
        return SA_MODE_CHARGING          # can't read -> protect the battery
    pv = snapshot.get("pvW") or 0
    soc = snapshot.get("batterySoc")
    min_pv = float(SETTINGS.get("solarMinPvW") or 0)
    min_soc = float(SETTINGS.get("solarMinBatterySoc") or 0)
    sunny = pv >= min_pv
    charged = soc is None or soc >= min_soc
    # Enough sun and battery healthy -> let the car share solar from the board.
    if sunny and charged:
        return SA_MODE_IDLE
    return SA_MODE_CHARGING


async def sync_inverter_to_charging(charging: bool) -> None:
    """Drive the inverter work mode to match the charging state. When charging,
    the mode is solar-aware (see _choose_charging_mode). When idle, the battery
    is always shared to the whole board. No-op unless configured and enabled."""
    if not SA_CLIENT or not SA_AUTO["enabled"]:
        return
    target = await _choose_charging_mode() if charging else SA_MODE_IDLE
    await SA_CLIENT.set_work_mode(target)


def _any_active_charging() -> bool:
    """True if any connector anywhere has a live transaction that has not been
    finalised — i.e. a car is actively drawing. Used so the inverter only
    returns to idle mode once nothing is charging."""
    for cp in CHARGERS.values():
        for conn in cp.connectors.values():
            txn_id = conn.get("transactionId")
            if not txn_id:
                continue
            txn = cp.transactions.get(txn_id)
            if txn and not txn.get("finalised"):
                return True
    return False

# --------------------------------------------------------------------------
# Dashboard sign-in. Set OCPP_ADMIN_USER / OCPP_ADMIN_PASSWORD in the
# environment. If no password is set, one is generated and printed to the log
# at startup so the app is never accidentally left open.
# --------------------------------------------------------------------------
# Comma-separated list of charge point ids permitted to connect. Empty means
# any id is accepted, which is fine on a private LAN but not on the internet.
ALLOWED_CHARGE_POINTS = {
    cp.strip() for cp in os.environ.get("OCPP_ALLOWED_CHARGE_POINTS", "").split(",")
    if cp.strip()
}
CP_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,48}$")

ADMIN_USER = os.environ.get("OCPP_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("OCPP_ADMIN_PASSWORD", "").strip()
# Set to 1 once you are behind HTTPS, so the cookie is never sent in the clear.
COOKIE_SECURE = os.environ.get("OCPP_COOKIE_SECURE", "0") == "1"

# Serve the dashboard over HTTPS when both files are present. Generated by
# make-cert.sh, or point these at a real certificate.
# Default to the writable data volume so a certificate can be generated without
# copying files into the container. Point these elsewhere to use your own.
TLS_CERT = os.environ.get("OCPP_TLS_CERT", "/app/data/certs/server.crt")
TLS_KEY = os.environ.get("OCPP_TLS_KEY", "/app/data/certs/server.key")
# Generate a self-signed certificate on first start when none exists.
AUTO_CERT = os.environ.get("OCPP_AUTO_CERT", "1") == "1"
TLS_ENABLED = False          # decided in prepare_tls()

# Set when DSM's reverse proxy (or any other) terminates TLS in front of this.
# The app then speaks plain HTTP on the LAN while the browser is on HTTPS.
TRUST_PROXY = os.environ.get("OCPP_TRUST_PROXY", "0") == "1"

# Public hostnames the dashboard is reached by. A reverse proxy may rewrite the
# Host header, which would otherwise make every request look cross-origin.
TRUSTED_ORIGINS = {
    origin.strip().rstrip("/").split("//")[-1].lower()
    for origin in os.environ.get("OCPP_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
}
SESSION_TTL = 8 * 3600
# Opt-in longer session, for the home screen app on a personal device.
REMEMBER_TTL = 30 * 24 * 3600
SESSION_COOKIE = "ocpp_session"

# Bump when you change the dashboard, so /version tells you what is deployed.
BUILD = "2026-07-28-v53"

# Tariff and schedule settings. Persisted to /app/data if that directory is
# writable, otherwise kept in memory and lost on restart.
SETTINGS_PATH = "/app/data/settings.json"
SETTINGS: Dict[str, Any] = {
    "pricePerKwh": float(os.environ.get("OCPP_PRICE_PER_KWH", "3.55")),
    "currency": os.environ.get("OCPP_CURRENCY", "R"),
    "schedule": None,
    # Vehicle profile. The charger cannot read state of charge over AC (that
    # needs ISO 15118), so SOC is derived from delivered energy against a
    # battery size and a starting point the user supplies.
    "batteryKwh": float(os.environ.get("OCPP_BATTERY_KWH", "60")),
    "targetSoc": float(os.environ.get("OCPP_TARGET_SOC", "80")),
    "startSoc": None,
    "chargingEfficiency": 0.9,
    # Auto-start. OCPP has no "begin charging at 22:00" command: a charging
    # profile can only cap current inside a transaction that already exists.
    # So the server watches for a plugged-in connector and starts it itself.
    #   off      - never
    #   plugged  - as soon as the cable is in the car
    #   schedule - as above, but only inside the schedule window
    "autoStart": "off",
    "autoStartIdTag": "AUTO",
    # Solar-aware charging. While a car is charging, share the battery to the
    # whole board (Zero export to CT) so the car can pull surplus solar — but
    # only when there is enough sun AND the battery is above a floor. Otherwise
    # keep the battery on the essential load. These two are user-adjustable.
    "solarMinPvW": float(os.environ.get("SOLAR_MIN_PV_W", "5000")),
    "solarMinBatterySoc": float(os.environ.get("SOLAR_MIN_BATTERY_SOC", "40")),
}


PERSISTENT = False        # set by check_persistence() at startup
_warned_once = False


def _warn_not_persisted(exc: Exception) -> None:
    global _warned_once
    if not _warned_once:
        _warned_once = True
        log.warning("Cannot write to %s (%s). Tariff, schedule and session "
                    "history will be lost on restart.",
                    os.path.dirname(SETTINGS_PATH), exc)


def check_persistence() -> None:
    """Write a probe file so a read-only data directory is obvious at startup
    rather than silently discarding history months later."""
    global PERSISTENT
    directory = os.path.dirname(SETTINGS_PATH)
    probe = os.path.join(directory, ".write-test")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        PERSISTENT = True
        log.info("Session history and settings persist to %s", directory)
    except OSError as exc:
        PERSISTENT = False
        log.warning("=" * 70)
        log.warning("%s is not writable (%s).", directory, exc)
        log.warning("Session history, tariff and schedule will reset on every")
        log.warning("restart. Use the named volume from docker-compose.yml")
        log.warning("rather than a ./data bind mount.")
        log.warning("=" * 70)


def load_settings() -> None:
    try:
        with open(SETTINGS_PATH) as fh:
            SETTINGS.update(json.load(fh))
            log.info("settings loaded from %s", SETTINGS_PATH)
    except (OSError, ValueError):
        pass


def save_settings() -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as fh:
            json.dump(SETTINGS, fh)
    except OSError as exc:
        _warn_not_persisted(exc)


# Completed sessions, kept for the 7-day summary. Persisted alongside settings.
HISTORY_PATH = "/app/data/sessions.json"
HISTORY: List[Dict[str, Any]] = []
HISTORY_MAX = int(os.environ.get("OCPP_HISTORY_MAX", "100000"))


# One-time recovery: mount an old Docker volume at /app/legacy and its contents
# are copied into the data folder on the next start.
LEGACY_DIR = os.environ.get("OCPP_LEGACY_DIR", "/app/legacy")


def migrate_legacy_data() -> None:
    """Copy history and settings out of an older mount, if one is provided and
    the current data folder is still empty."""
    if not os.path.isdir(LEGACY_DIR):
        return
    import shutil
    for source_name, target in (("sessions.json", HISTORY_PATH),
                                ("settings.json", SETTINGS_PATH)):
        source = os.path.join(LEGACY_DIR, source_name)
        if not os.path.isfile(source):
            continue
        if os.path.isfile(target) and os.path.getsize(target) > 2:
            log.info("%s already present, leaving it alone", source_name)
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            log.warning("Recovered %s from %s", source_name, LEGACY_DIR)
        except OSError as exc:
            log.warning("Could not recover %s: %s", source_name, exc)

    legacy_certs = os.path.join(LEGACY_DIR, "certs")
    target_certs = os.path.dirname(TLS_CERT)
    if os.path.isdir(legacy_certs) and not os.path.isfile(TLS_CERT):
        try:
            os.makedirs(target_certs, exist_ok=True)
            for name in os.listdir(legacy_certs):
                shutil.copyfile(os.path.join(legacy_certs, name),
                                os.path.join(target_certs, name))
            log.warning("Recovered the TLS certificate from %s", LEGACY_DIR)
        except OSError as exc:
            log.warning("Could not recover certificates: %s", exc)


def load_history() -> None:
    try:
        with open(HISTORY_PATH) as fh:
            HISTORY.extend(json.load(fh))
            log.info("%d past sessions loaded", len(HISTORY))
    except (OSError, ValueError):
        pass
    seed_history()
    # Resume transaction ids ABOVE the highest one already recorded, so a restart
    # never re-issues an id that a stored session still uses. Without this the
    # counter restarts at 1 each boot and a new session can overwrite an older
    # one that happened to share the id.
    global _txn_counter
    highest = 0
    for entry in HISTORY:
        try:
            highest = max(highest, int(entry.get("transactionId") or 0))
        except (TypeError, ValueError):
            continue
    _txn_counter = itertools.count(highest + 1)
    if highest:
        log.info("transaction ids resume from %d", highest + 1)


def seed_history() -> None:
    """One-time import of a bundled session file. If data/seed-sessions.json is
    present, its sessions are merged in with the same de-duplication used by the
    dashboard restore, then the file is renamed so it never runs again. This lets
    a charger's own exported history be shipped in without overwriting anything
    already recorded live."""
    seed_candidates = [
        os.path.join(os.path.dirname(HISTORY_PATH), "seed-sessions.json"),
        os.path.join(os.getcwd(), "seed-sessions.json"),
        "/app/seed-sessions.json",
    ]
    seed_path = next((p for p in seed_candidates if os.path.exists(p)), None)
    if not seed_path:
        return
    try:
        with open(seed_path) as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("seed file present but unreadable: %s", exc)
        return

    incoming = payload.get("sessions", payload) if isinstance(payload, dict) else payload
    if not isinstance(incoming, list):
        log.warning("seed file has no session list; skipping")
        return

    def keys(entry):
        stamp = entry.get("startedAt")
        minute = None
        if stamp:
            try:
                minute = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) \
                    .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                minute = None
        energy = round((entry.get("energyWh") or 0) / 100) * 100
        return (entry.get("transactionId"), stamp), ((minute, energy) if minute else None)

    seen_exact, seen_fuzzy = set(), set()
    for e in HISTORY:
        ex, fz = keys(e)
        seen_exact.add(ex)
        if fz:
            seen_fuzzy.add(fz)

    added = 0
    for e in incoming:
        if not isinstance(e, dict):
            continue
        ex, fz = keys(e)
        if ex in seen_exact or (fz and fz in seen_fuzzy):
            continue
        seen_exact.add(ex)
        if fz:
            seen_fuzzy.add(fz)
        HISTORY.append(e)
        added += 1

    HISTORY.sort(key=lambda e: str(e.get("startedAt") or ""))
    del HISTORY[:-HISTORY_MAX]
    if added:
        save_history()
    log.info("seed import: %d new sessions merged (%d already present)",
             added, len(incoming) - added)

    # Rename so it never re-imports, even across restarts.
    try:
        os.rename(seed_path, seed_path + ".imported")
    except OSError:
        pass


def save_history() -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w") as fh:
            json.dump(HISTORY[-HISTORY_MAX:], fh)
    except OSError as exc:
        _warn_not_persisted(exc)


SESSIONS: Dict[str, float] = {}          # token -> expiry timestamp
LOGIN_ATTEMPTS: Dict[str, List[float]] = {}  # client ip -> recent failures
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300


def record(charge_point_id: str, kind: str, detail: Any = None) -> None:
    entry = {"ts": utcnow(), "chargePointId": charge_point_id, "type": kind, "detail": detail}
    EVENTS.appendleft(entry)
    log.info("%s  %s  %s", charge_point_id, kind, detail if detail is not None else "")


# --------------------------------------------------------------------------
# Charge point session
# --------------------------------------------------------------------------
class ChargePoint(BaseChargePoint):
    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        self.connected_since = utcnow()
        self.last_seen = utcnow()
        self.boot: Dict[str, Any] = {}
        self.connectors: Dict[int, Dict[str, Any]] = {}
        self.transactions: Dict[int, Dict[str, Any]] = {}
        self.configuration: Dict[str, Any] = {}
        self.mode: Optional[str] = None          # regular | solar | eco | eco_plus
        self.solar_enabled: Optional[str] = None
        self.vendor_data: List[Dict[str, Any]] = []
        self.measurands_seen: set = set()

    # ---- snapshot for the API ------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "connectedSince": self.connected_since,
            "lastSeen": self.last_seen,
            "boot": self.boot,
            "connectors": self.connectors,
            "transactions": self.transactions,
            "configuration": self.configuration,
            "mode": self.mode,
            "solarEnabled": self.solar_enabled,
            "measurandsSeen": sorted(self.measurands_seen),
            "vendorData": self.vendor_data[-5:],
        }

    def _touch(self) -> None:
        self.last_seen = utcnow()

    def _connector(self, connector_id: int) -> Dict[str, Any]:
        return self.connectors.setdefault(
            connector_id,
            {"connectorId": connector_id, "status": "Unknown", "errorCode": "NoError",
             "meter": {}, "transactionId": None, "history": [], "session": None},
        )

    def _reading(self, conn: Dict[str, Any], *names) -> Optional[float]:
        for name in names:
            entry = conn["meter"].get(name)
            if entry and entry.get("value") not in (None, ""):
                try:
                    return float(entry["value"])
                except (TypeError, ValueError):
                    continue
        return None

    def _recompute_session(self, connector_id: int) -> None:
        """Derive the numbers the dashboard shows from the raw meter readings."""
        conn = self._connector(connector_id)
        amps = self._reading(conn, "Current.Import", "Current.Import.L1")
        volts = self._reading(conn, "Voltage", "Voltage.L1")
        watts = self._reading(conn, "Power.Active.Import")
        if watts is None and amps is not None and volts is not None:
            watts = amps * volts          # single phase: P = V x I
        energy_now = self._reading(conn, "Energy.Active.Import.Register")

        # Solar contribution. OCPP 1.6's measurand list is a fixed enum with no
        # solar entry, so there is no standard way for a charger to report this.
        # Best available sources, in order of trustworthiness:
        #   1. a vendor measurand mentioning solar (only if the charger sends one)
        #   2. Power.Active.Export — site surplus being exported, which is what
        #      the solar modes actually chase
        #   3. inference: in solar-only mode everything drawn came from surplus
        solar_w, solar_src = None, None
        for key in conn["meter"]:
            if "solar" in key.lower():
                solar_w, solar_src = self._reading(conn, key), "measurand"
                break
        if solar_w is None:
            export_w = self._reading(conn, "Power.Active.Export")
            if export_w is not None:
                solar_w, solar_src = export_w, "export"
        if solar_w is None and watts is not None and self.mode == "solar":
            solar_w, solar_src = watts, "inferred"
        elif solar_w is None and self.mode in ("eco", "eco_plus"):
            solar_src = "unknown"     # charger does not report the grid/solar split

        txn = self.transactions.get(conn.get("transactionId"))
        if txn:
            started = txn.get("startedAt")
            elapsed = None
            if started:
                try:
                    began = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    elapsed = max(0, int((datetime.now(timezone.utc) - began).total_seconds()))
                except ValueError:
                    elapsed = None
            delivered = None
            if energy_now is not None and txn.get("meterStart") is not None:
                delivered = max(0.0, energy_now - float(txn["meterStart"]))
            # State of charge. If the vehicle reports it over ISO 15118 the
            # charger may send an SoC measurand, which we trust outright.
            # Otherwise estimate from delivered energy against the battery size.
            soc = self._reading(conn, "SoC")
            soc_source = "vehicle" if soc is not None else None
            battery = float(SETTINGS.get("batteryKwh") or 0)
            start_soc = SETTINGS.get("startSoc")
            efficiency = float(SETTINGS.get("chargingEfficiency") or 0.9)
            if soc is None and delivered is not None and battery and start_soc is not None:
                added = (delivered / 1000) * efficiency / battery * 100
                soc = min(100.0, round(float(start_soc) + added, 1))
                soc_source = "estimated"

            target_soc = float(SETTINGS.get("targetSoc") or 0)
            energy_to_target = None
            if battery and target_soc and soc is not None:
                remaining = max(0.0, target_soc - soc)
                energy_to_target = round(battery * remaining / 100 / efficiency, 2)

            conn["session"] = {
                "transactionId": txn["transactionId"],
                "idTag": txn.get("idTag"),
                "startedAt": started,
                "elapsedSeconds": elapsed,
                "energyWh": delivered,
                "powerW": watts,
                "currentA": amps,
                "voltageV": volts,
                "temperatureC": self._reading(conn, "Temperature"),
                "offeredA": self._reading(conn, "Current.Offered"),
                "solarW": solar_w,
                "solarSource": solar_src,
                "mode": self.mode,
                "socPercent": soc,
                "socSource": soc_source,
                "targetSoc": target_soc or None,
                "startSoc": start_soc,
                "batteryKwh": battery or None,
                "energyToTargetKwh": energy_to_target,
            }
        else:
            conn["session"] = None

        if watts is not None:
            conn["history"].append({"t": utcnow(), "w": round(watts)})
            del conn["history"][:-120]

    # ---- inbound messages from the charger ------------------------------
    @on("BootNotification")
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        self._touch()
        self.boot = {"vendor": charge_point_vendor, "model": charge_point_model, **kwargs}
        record(self.id, "BootNotification", self.boot)
        # Ask the charger to report its current status right away, so the
        # dashboard shows the connector and its state without the operator having
        # to click "Bring online" after a reconnect or container restart.
        asyncio.create_task(self._request_status_after_boot())
        return result("BootNotification", current_time=utcnow(),
                      interval=HEARTBEAT_INTERVAL, status="Accepted")

    async def _request_status_after_boot(self):
        # Small delay so the charger finishes its boot handshake first.
        await asyncio.sleep(2)
        self._last_status_trigger = time.time()
        try:
            await self.call(request("TriggerMessage", requested_message="StatusNotification"))
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("status trigger after boot failed: %s", exc)

    async def _request_status_on_connect(self):
        # Wait for the message loop to be running, and long enough that if the
        # charger is also going to send a BootNotification (a real power-cycle),
        # that path handles this instead and this one steps aside.
        await asyncio.sleep(6)
        if time.time() - getattr(self, "_last_status_trigger", 0) < 8:
            return  # boot path already handled it
        self._last_status_trigger = time.time()
        # A charger that reconnects after a reboot can come back in a state where
        # it is connected but not reporting, and a bare status request is not
        # always enough to revive it — the reliable nudge is ChangeAvailability
        # -> Operative (what the "Bring online" button sends). Do that first,
        # UNLESS the operator deliberately took the charger offline, in which case
        # that choice is respected.
        if not OFFLINE_INTENT.get(self.id, False):
            try:
                await self.call(request("ChangeAvailability",
                                        connector_id=0, type="Operative"))
                log.info("%s: set operative on reconnect", self.id)
            except Exception as exc:  # noqa: BLE001 - best effort
                log.debug("operative on connect failed: %s", exc)
        try:
            await self.call(request("TriggerMessage", requested_message="StatusNotification"))
            log.info("%s: requested status on reconnect", self.id)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("status trigger on connect failed: %s", exc)
        # A charger that rebooted loses its recurring charging profile, so the
        # schedule's out-of-window current cap would silently vanish and the car
        # could charge full-power at any hour. Re-assert the schedule on the
        # charger if we're in schedule mode with a saved schedule.
        if SETTINGS.get("autoStart") == "schedule" and SETTINGS.get("schedule"):
            try:
                await apply_schedule_profile(self)
                log.info("%s: schedule re-applied on reconnect", self.id)
            except Exception as exc:  # noqa: BLE001 - best effort
                log.debug("schedule re-apply on connect failed: %s", exc)

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        self._touch()
        return result("Heartbeat", current_time=utcnow())

    @on("StatusNotification")
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        self._touch()
        conn = self._connector(connector_id)
        previous = conn.get("status")
        conn["status"] = status
        conn["errorCode"] = error_code
        conn["statusUpdatedAt"] = utcnow()
        if kwargs.get("vendor_error_code"):
            conn["vendorErrorCode"] = kwargs["vendor_error_code"]
        record(self.id, "StatusNotification", {"connectorId": connector_id, "status": status,
                                               "errorCode": error_code})

        # A session should close on whichever end-of-charging event happens
        # first, so the recorded end is always the true end. Three signals, all
        # while a transaction is open:
        #
        #   SuspendedEV            the CAR stopped drawing — target SOC reached
        #   Finishing              the charge cycle completed at the charger
        #   Available / Preparing  the car was unplugged (some firmware omits the
        #                          StopTransaction, so catch it here too)
        #
        # SuspendedEVSE is deliberately NOT here: that is the CHARGER pausing for
        # a 0 A schedule window, not the car finishing. The schedule window
        # closing is handled by the auto-start watcher issuing a real stop.
        END_OF_CHARGE = {
            "SuspendedEV": "SOCLimitReached",
            "Finishing": "Local",
            "Available": "EVDisconnected",
            "Preparing": "EVDisconnected",
        }
        if (status in END_OF_CHARGE and previous not in END_OF_CHARGE
                and conn.get("transactionId") and FINALISE_ON_SUSPENDED_EV):
            timestamp = kwargs.get("timestamp") or utcnow()
            self._finalise_session(connector_id, timestamp,
                                   reason=END_OF_CHARGE[status])

        # Cable unplugged (Available/Preparing after a session): clear any
        # autostart back-off so replugging a car starts promptly rather than
        # waiting out the long post-rejection cadence.
        if status in ("Available", "Preparing"):
            _autostart_backoff.pop(f"{self.id}:{connector_id}", None)
            _autostart_attempts.pop(f"{self.id}:{connector_id}", None)

        # If the car resumes after having been finalised (surplus returns, or the
        # battery drops below target), let the ongoing transaction record again
        # from here, so the extra energy is not lost.
        if status == "Charging":
            txn_id = conn.get("transactionId")
            txn = self.transactions.get(txn_id) if txn_id else None
            if txn and txn.get("finalised"):
                txn["finalised"] = False
                txn["resumedAt"] = kwargs.get("timestamp") or utcnow()
            # Car is drawing again (whether a fresh resume or steady charging):
            # keep the battery on the essential load.
            if txn:
                asyncio.create_task(sync_inverter_to_charging(True))

        return result("StatusNotification")

    def _finalise_session(self, connector_id, timestamp, reason=None,
                          meter_stop=None):
        """Write a completed session to history. Called both from a real
        StopTransaction and when the car reaches SOC (SuspendedEV). The session
        is dated by when it STARTED, so an overnight charge lands on its start
        day rather than jumping to the next date at midnight."""
        conn = self._connector(connector_id)
        txn_id = conn.get("transactionId")
        txn = self.transactions.get(txn_id) if txn_id else None
        if not txn or txn.get("finalised"):
            return

        # Best available final reading: an explicit meter_stop, else the latest
        # absolute meter register seen during the session. The register is the
        # total lifetime meter value, so it IS the stop reading directly.
        if meter_stop is None:
            reading = self._reading(conn, "Energy.Active.Import.Register")
            if reading is not None and reading >= txn["meterStart"]:
                meter_stop = reading
        if meter_stop is None:
            meter_stop = txn["meterStart"] + (txn.get("energyWh") or 0)

        txn["meterStop"] = meter_stop
        txn["stoppedAt"] = timestamp
        txn["reason"] = reason
        txn["energyWh"] = max(0, meter_stop - txn["meterStart"])
        txn["finalised"] = True

        duration = None
        try:
            began = datetime.fromisoformat(str(txn["startedAt"]).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            duration = max(0, int((ended - began).total_seconds()))
        except (ValueError, AttributeError, KeyError, TypeError):
            pass

        kwh = (txn["energyWh"] or 0) / 1000
        price = float(SETTINGS.get("pricePerKwh") or 0)
        entry = {
            "chargePointId": self.id,
            "transactionId": txn_id,
            "idTag": txn.get("idTag"),
            # startedAt is the anchor everywhere — usage list, graphs, dedup.
            "startedAt": txn.get("startedAt"),
            "stoppedAt": timestamp,
            "energyWh": txn["energyWh"],
            "durationSeconds": duration,
            "pricePerKwh": price,
            "cost": round(kwh * price, 2),
            "mode": self.mode,
            "reason": reason,
            "peakW": max((p["w"] for p in conn.get("history", [])), default=None),
        }

        # If this exact transaction was already written (car reached SOC, then
        # resumed, now finishing again), update that row in place rather than
        # duplicate it. Match on BOTH the id AND the start time: a restart can
        # re-use a transaction id, and matching on id alone would let a new
        # session overwrite an unrelated older one that shared the id.
        for i, existing in enumerate(HISTORY):
            if (existing.get("transactionId") == txn_id
                    and existing.get("startedAt") == txn.get("startedAt")):
                HISTORY[i] = entry
                break
        else:
            HISTORY.append(entry)
        del HISTORY[:-HISTORY_MAX]
        save_history()
        self._recompute_session(connector_id)
        record(self.id, "SessionRecorded",
               {"transactionId": txn_id, "reason": reason,
                "energyWh": txn["energyWh"], "durationSeconds": duration})
        # Charging has ended (SOC reached, cable out, window closed, or cycle
        # done). Return the inverter to sharing the battery with the whole board,
        # unless another connector is still actively charging.
        if not _any_active_charging():
            asyncio.create_task(sync_inverter_to_charging(False))

    @on("Authorize")
    async def on_authorize(self, id_tag, **kwargs):
        self._touch()
        status = "Accepted" if (ACCEPT_UNKNOWN_TAGS or id_tag in ALLOWED_TAGS) else "Invalid"
        record(self.id, "Authorize", {"idTag": id_tag, "status": status})
        return result("Authorize", id_tag_info={"status": status})

    @on("StartTransaction")
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        self._touch()
        txn_id = next(_txn_counter)
        self.transactions[txn_id] = {
            "transactionId": txn_id, "connectorId": connector_id, "idTag": id_tag,
            "meterStart": meter_start, "startedAt": timestamp, "stoppedAt": None,
            "meterStop": None, "energyWh": None,
        }
        self._connector(connector_id)["transactionId"] = txn_id
        self._connector(connector_id)["history"] = []
        self._recompute_session(connector_id)
        record(self.id, "StartTransaction", {"transactionId": txn_id, "connectorId": connector_id,
                                             "idTag": id_tag, "meterStart": meter_start})
        # Car is now drawing: keep the battery on the essential load, not shared
        # to the whole board. Best-effort, never blocks the charge.
        asyncio.create_task(sync_inverter_to_charging(True))
        return result("StartTransaction", transaction_id=txn_id, id_tag_info={"status": "Accepted"})

    @on("StopTransaction")
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        self._touch()
        txn = self.transactions.get(transaction_id)
        if txn:
            connector_id = txn["connectorId"]
            if txn.get("finalised"):
                # Already recorded when the car reached SOC and it did not resume.
                # Refresh the final meter reading in place, no new row.
                txn["finalised"] = False   # allow _finalise to update the entry
                self._finalise_session(connector_id, txn.get("stoppedAt") or timestamp,
                                       reason=txn.get("reason") or "SOCLimitReached",
                                       meter_stop=max(meter_stop, txn.get("meterStop") or 0))
            else:
                self._finalise_session(connector_id, timestamp,
                                       reason=kwargs.get("reason"),
                                       meter_stop=meter_stop)
            self._connector(connector_id)["transactionId"] = None
            self._recompute_session(connector_id)
        record(self.id, "StopTransaction", {"transactionId": transaction_id, "meterStop": meter_stop,
                                            "reason": kwargs.get("reason")})
        return result("StopTransaction", id_tag_info={"status": "Accepted"})

    @on("MeterValues")
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        self._touch()
        conn = self._connector(connector_id)
        readings: Dict[str, Any] = {}
        for sample in meter_value:
            for value in sample.get("sampled_value", sample.get("sampledValue", [])):
                measurand = value.get("measurand", "Energy.Active.Import.Register")
                phase = value.get("phase")
                key = f"{measurand}.{phase}" if phase else measurand
                self.measurands_seen.add(key)
                readings[key] = {"value": value.get("value"), "unit": value.get("unit"),
                                 "ts": sample.get("timestamp")}
        conn["meter"].update(readings)
        conn["meterUpdatedAt"] = utcnow()
        self._recompute_session(connector_id)
        return result("MeterValues")

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id, **kwargs):
        self._touch()
        self.vendor_data.append({"ts": utcnow(), "vendorId": vendor_id, **kwargs})
        del self.vendor_data[:-20]
        record(self.id, "DataTransfer", {"vendorId": vendor_id, **kwargs})
        return result("DataTransfer", status="Accepted")

    @on("DiagnosticsStatusNotification")
    async def on_diagnostics_status(self, status, **kwargs):
        record(self.id, "DiagnosticsStatusNotification", status)
        return result("DiagnosticsStatusNotification")

    @on("FirmwareStatusNotification")
    async def on_firmware_status(self, status, **kwargs):
        record(self.id, "FirmwareStatusNotification", status)
        return result("FirmwareStatusNotification")


# --------------------------------------------------------------------------
# WebSocket endpoint the charger dials into
# --------------------------------------------------------------------------
async def on_connect(connection, path: Optional[str] = None):
    if path is None:  # websockets >= 14 moved the path onto the request object
        path = getattr(connection, "path", None) or connection.request.path
    # Teltonika's URL field must end in "/", so the charger may dial "//EVC121".
    cp_id = unquote(path.strip("/").split("/")[-1] or "")

    # The id is shown in the dashboard and used as a dictionary key, so reject
    # anything that is not a plain identifier rather than sanitising it.
    if not CP_ID_PATTERN.match(cp_id):
        log.warning("rejected charge point id %r", cp_id[:80])
        await connection.close(1008, "invalid charge point id")
        return

    if ALLOWED_CHARGE_POINTS and cp_id not in ALLOWED_CHARGE_POINTS:
        log.warning("charge point %s is not in the allowlist", cp_id)
        await connection.close(1008, "charge point not permitted")
        return

    if connection.subprotocol != "ocpp1.6":
        log.warning("%s connected without the ocpp1.6 subprotocol — closing", cp_id)
        await connection.close(1002, "ocpp1.6 subprotocol required")
        return

    # SecurityProfile 1 or 2 on the charger means HTTP Basic auth, where the
    # username is the charge point id and the password is AuthorizationKey.
    if AUTHORIZATION_KEY:
        headers = getattr(getattr(connection, "request", None), "headers", {}) or {}
        header = headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(
            f"{cp_id}:{AUTHORIZATION_KEY}".encode()).decode()
        if not hmac.compare_digest(header, expected):
            log.warning("%s failed Basic auth — closing", cp_id)
            await connection.close(1008, "unauthorized")
            return

    cp = ChargePoint(cp_id, connection)
    CHARGERS[cp_id] = cp
    record(cp_id, "Connected")
    # Ask the charger to report its current status as soon as it connects — this
    # covers a container restart where the charger's WebSocket reconnects but it
    # doesn't power-cycle (so no BootNotification fires). Without this the
    # dashboard would show the connector as unknown until the operator clicked
    # "Bring online". Best-effort and slightly delayed so the charger settles.
    asyncio.create_task(cp._request_status_on_connect())
    try:
        await cp.start()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if CHARGERS.get(cp_id) is cp:
            del CHARGERS[cp_id]
        record(cp_id, "Disconnected")
        # If the charger drops mid-session, the normal end-of-charge events never
        # fire, which would leave the inverter stuck on the charging work mode
        # (battery on load only) indefinitely. Reconcile now: with this charger
        # gone, if nothing else is charging, return the inverter to idle (share
        # to the whole board). Best-effort — never raises into the finally.
        try:
            if not _any_active_charging():
                asyncio.create_task(sync_inverter_to_charging(False))
        except Exception as exc:  # noqa: BLE001
            log.warning("inverter reconcile on disconnect failed: %s", exc)


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------
api = FastAPI(title="JTech Grid Control", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------
# Security headers. The pages carry one inline <style> and one inline <script>,
# so the CSP pins their SHA-256 hashes instead of allowing 'unsafe-inline'.
# Hashes are recomputed if the files change on disk.
# --------------------------------------------------------------------------
_CSP_UNSET = object()
_csp_cache: Dict[str, Any] = {"key": _CSP_UNSET, "value": None}


def _inline_hashes() -> List[str]:
    hashes = []
    for path in ("static/index.html", "static/login.html"):
        try:
            page = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for tag in ("script", "style"):
            for block in re.findall(rf"<{tag}>(.*?)</{tag}>", page, re.S):
                digest = hashlib.sha256(block.encode()).digest()
                hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


# If a hash ever mismatches, the dashboard would render with no styling or
# scripts at all. Set OCPP_STRICT_CSP=0 to fall back to a permissive policy.
STRICT_CSP = os.environ.get("OCPP_STRICT_CSP", "1") != "0"


PERMISSIVE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; "
                  "style-src 'unsafe-inline'; img-src 'self' data:; "
                  "manifest-src 'self'; connect-src 'self'; "
                  "frame-ancestors 'none'; base-uri 'none'")


def content_security_policy() -> str:
    if not STRICT_CSP:
        return PERMISSIVE_CSP
    try:
        key = tuple(os.path.getmtime(p) for p in
                    ("static/index.html", "static/login.html"))
    except OSError:
        key = None
    if _csp_cache["key"] is _CSP_UNSET or _csp_cache["key"] != key:
        hashes = _inline_hashes()
        _csp_cache["key"] = key
        if not hashes:
            # Pages unreadable from the working directory. Serving no policy at
            # all would 500 every request, so fall back rather than break.
            log.warning("Could not hash the inline blocks for the CSP; "
                        "falling back to a permissive policy")
            _csp_cache["value"] = PERMISSIVE_CSP
            return _csp_cache["value"]
        inline = " ".join(hashes)
        _csp_cache["value"] = (
            "default-src 'none'; "
            f"script-src {inline}; "
            f"style-src {inline}; "
            "img-src 'self' data:; "
            "manifest-src 'self'; "
            "connect-src 'self'; "
            "form-action 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )
    return _csp_cache["value"]


@api.middleware("http")
async def security_headers(request: Request, call_next):
    # Defence in depth against cross-site requests. SameSite=Lax already blocks
    # the cookie on cross-site POSTs; this rejects them outright.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            strip_port = lambda value: value.rsplit(":", 1)[0] if ":" in value else value
            origin_host = origin.split("//")[-1].rstrip("/").lower()
            host = request.headers.get("host", "").lower()
            forwarded = request.headers.get("x-forwarded-host", "").lower()
            allowed = {host, forwarded} | TRUSTED_ORIGINS
            allowed |= {strip_port(a) for a in allowed if a}
            if origin_host not in allowed and strip_port(origin_host) not in allowed:
                log.warning("refused cross-origin %s from %s (host %s)",
                            request.url.path, origin_host, host)
                return JSONResponse({"detail": "Cross-origin request refused"},
                                    status_code=403)

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = content_security_policy()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    https = TLS_ENABLED or (
        TRUST_PROXY and request.headers.get("x-forwarded-proto") == "https")
    if https or COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Behind a proxy every request appears to come from the proxy, so one
    attacker would rate-limit everyone. Only trust the header when told to."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else "unknown"


def purge_expired() -> None:
    now = time.time()
    for token in [t for t, expiry in SESSIONS.items() if expiry < now]:
        SESSIONS.pop(token, None)
    for ip in [i for i, times in LOGIN_ATTEMPTS.items()
               if not times or now - max(times) > ATTEMPT_WINDOW]:
        LOGIN_ATTEMPTS.pop(ip, None)
    if len(LOGIN_ATTEMPTS) > 5000:          # crude cap against memory pressure
        LOGIN_ATTEMPTS.clear()


def rate_limited(ip: str) -> bool:
    recent = [t for t in LOGIN_ATTEMPTS.get(ip, []) if time.time() - t < ATTEMPT_WINDOW]
    LOGIN_ATTEMPTS[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    expiry = SESSIONS.get(token)
    if not expiry or expiry < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


def require_session(request: Request) -> None:
    if not valid_session(request):
        raise HTTPException(401, "Sign in first")


router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])


class LoginBody(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=200)
    remember: bool = False


@api.post("/api/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    purge_expired()
    ip = client_ip(request)
    if rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Wait five minutes.")

    ok_user = hmac.compare_digest(body.username, ADMIN_USER)
    ok_pass = hmac.compare_digest(body.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())
        log.warning("failed sign-in from %s", ip)
        raise HTTPException(401, "Wrong username or password")

    LOGIN_ATTEMPTS.pop(ip, None)
    token = secrets.token_urlsafe(32)
    ttl = REMEMBER_TTL if body.remember else SESSION_TTL
    SESSIONS[token] = time.time() + ttl
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=ttl, path="/")
    log.info("signed in from %s", ip)
    return {"status": "ok", "user": ADMIN_USER}


@api.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    SESSIONS.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@api.get("/api/auth/me")
async def whoami(request: Request):
    if not valid_session(request):
        raise HTTPException(401, "Sign in first")
    return {"user": ADMIN_USER}


def get_cp(charge_point_id: str) -> ChargePoint:
    cp = CHARGERS.get(charge_point_id)
    if cp is None:
        raise HTTPException(404, f"{charge_point_id} is not connected")
    return cp


async def send(cp: ChargePoint, name: str, **kwargs) -> Dict[str, Any]:
    try:
        response = await cp.call(request(name, **kwargs))
    except asyncio.TimeoutError:
        raise HTTPException(504, f"{name}: the charger did not answer in time")
    payload = getattr(response, "__dict__", {"response": str(response)})
    record(cp.id, f"{name} -> ", payload)
    return payload


class StartBody(BaseModel):
    connectorId: int = Field(1, ge=0, le=8)
    idTag: str = Field("DEMO", min_length=1, max_length=20)


class StopBody(BaseModel):
    transactionId: int


class ResetBody(BaseModel):
    type: str = "Soft"  # Soft | Hard


class ConfigBody(BaseModel):
    key: str = Field(..., min_length=1, max_length=50)
    value: str = Field(..., max_length=500)


class AvailabilityBody(BaseModel):
    connectorId: int = 0  # 0 = the whole charge point
    type: str = "Operative"  # Operative | Inoperative


class LimitBody(BaseModel):
    connectorId: int = Field(1, ge=0, le=8)
    limitAmps: float = Field(32, ge=0, le=80)
    numberPhases: int = Field(1, ge=1, le=3)
    purpose: str = "TxDefaultProfile"  # or TxProfile, to limit a running session
    stackLevel: int = 0


class TriggerBody(BaseModel):
    requestedMessage: str = "StatusNotification"
    connectorId: Optional[int] = None


class ReserveBody(BaseModel):
    connectorId: int = 1
    idTag: str = "DEMO"
    reservationId: int = 1
    expiryDate: str  # ISO 8601


# Teltonika vendor keys, added in firmware 1.12. "Solar" enables the feature,
# "SolarCharging" picks the profile. Not part of the OCPP standard.
SOLAR_MODES = ("regular", "solar", "eco", "eco_plus")


class ModeBody(BaseModel):
    mode: str  # regular | solar | eco | eco_plus


class SolarBody(BaseModel):
    enabled: bool = True


class ScheduleBody(BaseModel):
    startHour: int = Field(22, ge=0, le=23)
    startMinute: int = Field(0, ge=0, le=59)
    endHour: int = Field(6, ge=0, le=23)
    endMinute: int = Field(0, ge=0, le=59)
    limitAmps: float = Field(32, ge=0, le=80)
    numberPhases: int = Field(1, ge=1, le=3)


class SettingsBody(BaseModel):
    pricePerKwh: Optional[float] = None
    currency: Optional[str] = None
    batteryKwh: Optional[float] = None
    targetSoc: Optional[float] = None
    startSoc: Optional[float] = None
    chargingEfficiency: Optional[float] = None
    autoStart: Optional[str] = None
    autoStartIdTag: Optional[str] = None


class LocalListBody(BaseModel):
    listVersion: int = 1
    updateType: str = "Full"  # Full | Differential
    idTags: List[str] = []


@router.get("/chargers")
async def list_chargers() -> List[Dict[str, Any]]:
    return [cp.snapshot() for cp in CHARGERS.values()]


@router.get("/chargers/{charge_point_id}")
async def get_charger(charge_point_id: str):
    return get_cp(charge_point_id).snapshot()


@router.get("/events")
async def get_events(limit: int = 100):
    return list(EVENTS)[:limit]


@router.post("/chargers/{charge_point_id}/start")
async def remote_start(charge_point_id: str, body: StartBody):
    return await send(get_cp(charge_point_id), "RemoteStartTransaction",
                      id_tag=body.idTag, connector_id=body.connectorId)


@router.post("/chargers/{charge_point_id}/stop")
async def remote_stop(charge_point_id: str, body: StopBody):
    return await send(get_cp(charge_point_id), "RemoteStopTransaction",
                      transaction_id=body.transactionId)


@router.post("/chargers/{charge_point_id}/reset")
async def reset(charge_point_id: str, body: ResetBody):
    return await send(get_cp(charge_point_id), "Reset", type=body.type)


@router.post("/chargers/{charge_point_id}/unlock")
async def unlock(charge_point_id: str, connector_id: int = 1):
    return await send(get_cp(charge_point_id), "UnlockConnector", connector_id=connector_id)


@router.get("/chargers/{charge_point_id}/configuration")
async def get_configuration(charge_point_id: str, keys: str = ""):
    cp = get_cp(charge_point_id)
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    payload = await send(cp, "GetConfiguration", key=key_list or None)
    known = payload.get("configuration_key") or payload.get("configurationKey") or []
    cp.configuration = {
        item["key"]: {"value": item.get("value"), "readonly": item.get("readonly")}
        for item in known
    }
    payload["unknown_key"] = payload.get("unknown_key") or payload.get("unknownKey")
    return payload


@router.post("/chargers/{charge_point_id}/configuration")
async def change_configuration(charge_point_id: str, body: ConfigBody):
    return await send(get_cp(charge_point_id), "ChangeConfiguration",
                      key=body.key, value=body.value)


@router.post("/chargers/{charge_point_id}/availability")
async def change_availability(charge_point_id: str, body: AvailabilityBody):
    cp = get_cp(charge_point_id)
    # Remember a deliberate offline so an automatic reconnect doesn't flip it
    # back to operative behind the operator's back. Stored by charger id so it
    # survives the charger reconnecting (which rebuilds the ChargePoint object).
    if body.connectorId == 0:
        OFFLINE_INTENT[charge_point_id] = (body.type == "Inoperative")
    return await send(cp, "ChangeAvailability",
                      connector_id=body.connectorId, type=body.type)


@router.post("/chargers/{charge_point_id}/limit")
async def set_limit(charge_point_id: str, body: LimitBody):
    """Cap the charging current with a TxDefaultProfile."""
    # The EVC121 advertises ChargingScheduleAllowedChargingRateUnit = "Current",
    # ChargeProfileMaxStackLevel = 5 and ChargingScheduleMaxPeriods = 5.
    profile = {
        "chargingProfileId": 1,
        "stackLevel": min(body.stackLevel, 5),
        "chargingProfilePurpose": body.purpose,
        "chargingProfileKind": "Relative",
        "chargingSchedule": {
            "chargingRateUnit": "A",
            "chargingSchedulePeriod": [
                {"startPeriod": 0, "limit": body.limitAmps, "numberPhases": body.numberPhases}
            ],
        },
    }
    return await send(get_cp(charge_point_id), "SetChargingProfile",
                      connector_id=body.connectorId, cs_charging_profiles=profile)


@router.post("/chargers/{charge_point_id}/clear-limit")
async def clear_limit(charge_point_id: str):
    return await send(get_cp(charge_point_id), "ClearChargingProfile", id=1)


@router.post("/chargers/{charge_point_id}/clear-cache")
async def clear_cache(charge_point_id: str):
    """Empty the charger's local authorization cache (max 20 entries)."""
    return await send(get_cp(charge_point_id), "ClearCache")


@router.get("/chargers/{charge_point_id}/composite-schedule")
async def composite_schedule(charge_point_id: str, connector_id: int = 1, duration: int = 3600):
    """What the charger thinks it will deliver. Duration is capped at 24h."""
    return await send(get_cp(charge_point_id), "GetCompositeSchedule",
                      connector_id=connector_id, duration=min(duration, 86400),
                      charging_rate_unit="A")


@router.post("/chargers/{charge_point_id}/reserve")
async def reserve_now(charge_point_id: str, body: ReserveBody):
    return await send(get_cp(charge_point_id), "ReserveNow",
                      connector_id=body.connectorId, expiry_date=body.expiryDate,
                      id_tag=body.idTag, reservation_id=body.reservationId)


@router.post("/chargers/{charge_point_id}/cancel-reservation")
async def cancel_reservation(charge_point_id: str, reservation_id: int):
    return await send(get_cp(charge_point_id), "CancelReservation",
                      reservation_id=reservation_id)


@router.get("/chargers/{charge_point_id}/local-list-version")
async def local_list_version(charge_point_id: str):
    return await send(get_cp(charge_point_id), "GetLocalListVersion")


@router.post("/chargers/{charge_point_id}/local-list")
async def send_local_list(charge_point_id: str, body: LocalListBody):
    """Offline-capable RFID whitelist. Charger holds max 50 entries, 10 per push."""
    if len(body.idTags) > 10:
        raise HTTPException(400, "The charger accepts at most 10 entries per SendLocalList")
    return await send(get_cp(charge_point_id), "SendLocalList",
                      list_version=body.listVersion,
                      update_type=body.updateType,
                      local_authorization_list=[
                          {"idTag": tag, "idTagInfo": {"status": "Accepted"}}
                          for tag in body.idTags
                      ])


@router.post("/chargers/{charge_point_id}/diagnostics")
async def get_diagnostics(charge_point_id: str, location: str):
    """location is an upload target the charger can reach: HTTP, HTTPS or FTP."""
    return await send(get_cp(charge_point_id), "GetDiagnostics", location=location)


@router.post("/chargers/{charge_point_id}/firmware")
async def update_firmware(charge_point_id: str, location: str, retrieve_date: Optional[str] = None):
    return await send(get_cp(charge_point_id), "UpdateFirmware",
                      location=location, retrieve_date=retrieve_date or utcnow())


@router.get("/chargers/{charge_point_id}/mode")
async def get_mode(charge_point_id: str):
    """Read the charger's current solar charging mode."""
    payload = await send(get_cp(charge_point_id), "GetConfiguration",
                         key=["Solar", "SolarCharging"])
    known = payload.get("configuration_key") or payload.get("configurationKey") or []
    unknown = payload.get("unknown_key") or payload.get("unknownKey") or []
    values = {item["key"]: item.get("value") for item in known}
    cp = get_cp(charge_point_id)
    cp.mode = values.get("SolarCharging")
    cp.solar_enabled = values.get("Solar")
    return {
        "solarEnabled": cp.solar_enabled,
        "mode": cp.mode,
        "supported": not unknown,
        "unknownKeys": unknown,
    }


@router.post("/chargers/{charge_point_id}/mode")
async def set_mode(charge_point_id: str, body: ModeBody):
    """Switch between regular, solar-only, eco and eco+ charging."""
    if body.mode not in SOLAR_MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(SOLAR_MODES)}")
    cp = get_cp(charge_point_id)
    results = {}
    # The solar profiles only take effect with the feature switched on.
    if body.mode != "regular":
        results["Solar"] = await send(cp, "ChangeConfiguration", key="Solar", value="1")
    results["SolarCharging"] = await send(cp, "ChangeConfiguration",
                                          key="SolarCharging", value=body.mode)
    if results["SolarCharging"].get("status") == "Accepted":
        cp.mode = body.mode
    return {"mode": body.mode, "results": results}


@router.post("/chargers/{charge_point_id}/solar")
async def set_solar(charge_point_id: str, body: SolarBody):
    return await send(get_cp(charge_point_id), "ChangeConfiguration",
                      key="Solar", value="1" if body.enabled else "0")


# --------------------------------------------------------------------------
# Scheduling. OCPP 1.6 has no "start at 22:00" command. The way to do it is a
# daily recurring charging profile that allows current inside the window and
# 0 A outside it, which parks the charger in SuspendedEVSE until the window
# opens. The EVC121 allows at most 5 periods, and we use 3.
# --------------------------------------------------------------------------
SCHEDULE_PROFILE_ID = 2


@router.get("/settings")
async def get_settings():
    now = datetime.now().astimezone()
    return {**SETTINGS, "timezone": f"{now.tzname()} (UTC{now.strftime('%z')[:3]})",
            "serverTime": now.strftime("%H:%M")}


@router.post("/settings")
async def update_settings(body: SettingsBody):
    if body.pricePerKwh is not None:
        if body.pricePerKwh < 0:
            raise HTTPException(400, "Price cannot be negative")
        SETTINGS["pricePerKwh"] = round(body.pricePerKwh, 4)
    if body.currency:
        SETTINGS["currency"] = body.currency[:3]
    if body.batteryKwh is not None:
        if not 1 <= body.batteryKwh <= 300:
            raise HTTPException(400, "Battery capacity must be 1-300 kWh")
        SETTINGS["batteryKwh"] = round(body.batteryKwh, 1)
    if body.targetSoc is not None:
        if not 1 <= body.targetSoc <= 100:
            raise HTTPException(400, "Target SOC must be 1-100")
        SETTINGS["targetSoc"] = round(body.targetSoc, 1)
    if body.startSoc is not None:
        if not 0 <= body.startSoc <= 100:
            raise HTTPException(400, "Starting SOC must be 0-100")
        SETTINGS["startSoc"] = round(body.startSoc, 1)
    if body.autoStart is not None:
        if body.autoStart not in ("off", "plugged", "schedule"):
            raise HTTPException(400, "autoStart must be off, plugged or schedule")
        changed_mode = SETTINGS.get("autoStart") != body.autoStart
        previous_mode = SETTINGS.get("autoStart")
        SETTINGS["autoStart"] = body.autoStart
        # A deliberate mode change means "act on this now" — clear any retry
        # back-off and attempt timers left over from the previous mode.
        if changed_mode:
            _autostart_backoff.clear()
            _autostart_attempts.clear()
        # Keep the on-charger schedule cap in sync with the mode, every time this
        # is set (not only when the mode changes) — so pressing "In window" always
        # re-asserts the cap even if it went missing (e.g. the charger rebooted
        # and dropped the profile), and leaving schedule mode always removes it.
        # The saved schedule is never deleted, so nothing is lost.
        if SETTINGS.get("schedule"):
            for cp in list(CHARGERS.values()):
                if body.autoStart == "schedule":
                    await apply_schedule_profile(cp)
                elif previous_mode == "schedule" or changed_mode:
                    await remove_schedule_profile(cp)
    if body.autoStartIdTag:
        SETTINGS["autoStartIdTag"] = body.autoStartIdTag[:20]
    if body.chargingEfficiency is not None:
        if not 0.5 <= body.chargingEfficiency <= 1:
            raise HTTPException(400, "Efficiency must be between 0.5 and 1")
        SETTINGS["chargingEfficiency"] = round(body.chargingEfficiency, 3)
    save_settings()
    return SETTINGS


async def apply_schedule_profile(cp) -> bool:
    """Push the saved schedule to the charger as a recurring charging profile.
    Returns True if accepted. Does not change saved settings — it just puts the
    profile on the charger. Used when entering schedule mode."""
    sched = SETTINGS.get("schedule")
    if not sched:
        return False
    try:
        sh, sm = (int(x) for x in sched["start"].split(":"))
        eh, em = (int(x) for x in sched["end"].split(":"))
    except (KeyError, ValueError):
        return False
    start_s = sh * 3600 + sm * 60
    end_s = eh * 3600 + em * 60
    amps = sched.get("limitAmps", 32)
    phases = sched.get("numberPhases", 1)
    period = lambda at, a: {"startPeriod": at, "limit": a, "numberPhases": phases}
    if start_s < end_s:
        periods = [period(0, 0), period(start_s, amps), period(end_s, 0)]
    else:
        periods = [period(0, amps), period(end_s, 0), period(start_s, amps)]
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    profile = {
        "chargingProfileId": SCHEDULE_PROFILE_ID,
        "stackLevel": 1,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind": "Recurring",
        "recurrencyKind": "Daily",
        "chargingSchedule": {
            "duration": 86400,
            "startSchedule": midnight.astimezone(timezone.utc).isoformat(),
            "chargingRateUnit": "A",
            "chargingSchedulePeriod": periods,
        },
    }
    try:
        resp = await send(cp, "SetChargingProfile", connector_id=1,
                          cs_charging_profiles=profile)
        return resp.get("status") == "Accepted"
    except Exception as exc:  # noqa: BLE001
        log.warning("apply schedule profile failed: %s", exc)
        return False


async def remove_schedule_profile(cp) -> None:
    """Remove the schedule profile from the charger so it no longer caps current
    outside the window — WITHOUT deleting the saved schedule, so it can be
    re-applied later. Used when leaving schedule mode (e.g. switching to
    'On plug-in' for ad-hoc full-power charging like preconditioning)."""
    try:
        await send(cp, "ClearChargingProfile", id=SCHEDULE_PROFILE_ID)
        log.info("%s: schedule cap removed from charger (saved schedule kept)", cp.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("remove schedule profile failed: %s", exc)


@router.post("/chargers/{charge_point_id}/schedule")
async def set_schedule(charge_point_id: str, body: ScheduleBody):
    for value, name in ((body.startHour, "startHour"), (body.endHour, "endHour")):
        if not 0 <= value <= 23:
            raise HTTPException(400, f"{name} must be 0-23")
    for value, name in ((body.startMinute, "startMinute"), (body.endMinute, "endMinute")):
        if not 0 <= value <= 59:
            raise HTTPException(400, f"{name} must be 0-59")

    start_s = body.startHour * 3600 + body.startMinute * 60
    end_s = body.endHour * 3600 + body.endMinute * 60
    if start_s == end_s:
        raise HTTPException(400, "Start and end cannot be the same time")

    period = lambda at, amps: {"startPeriod": at, "limit": amps,
                               "numberPhases": body.numberPhases}
    if start_s < end_s:                       # window inside one day
        periods = [period(0, 0), period(start_s, body.limitAmps), period(end_s, 0)]
    else:                                     # window crosses midnight
        periods = [period(0, body.limitAmps), period(end_s, 0), period(start_s, body.limitAmps)]

    # Periods are offsets from startSchedule, so anchor it at local midnight and
    # send it as UTC. TZ in the container decides what "local" means.
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    profile = {
        "chargingProfileId": SCHEDULE_PROFILE_ID,
        "stackLevel": 1,                      # above the plain limit profile
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind": "Recurring",
        "recurrencyKind": "Daily",
        "chargingSchedule": {
            "duration": 86400,
            "startSchedule": midnight.astimezone(timezone.utc).isoformat(),
            "chargingRateUnit": "A",
            "chargingSchedulePeriod": periods,
        },
    }
    response = await send(get_cp(charge_point_id), "SetChargingProfile",
                          connector_id=1, cs_charging_profiles=profile)

    if response.get("status") == "Accepted":
        SETTINGS["schedule"] = {
            "start": f"{body.startHour:02d}:{body.startMinute:02d}",
            "end": f"{body.endHour:02d}:{body.endMinute:02d}",
            "limitAmps": body.limitAmps,
            "timezone": str(now.tzinfo),
        }
        save_settings()
    return {"response": response, "schedule": SETTINGS["schedule"], "periods": periods}


@router.post("/chargers/{charge_point_id}/schedule/clear")
async def clear_schedule(charge_point_id: str):
    response = await send(get_cp(charge_point_id), "ClearChargingProfile",
                          id=SCHEDULE_PROFILE_ID)
    SETTINGS["schedule"] = None
    save_settings()
    return response


@router.get("/history")
async def history(days: int = 7, start: Optional[str] = None,
                  end: Optional[str] = None):
    """Per-day totals for the summary strip, plus the whole-period rollup.

    Either pass `days` for a rolling window ending today, or an explicit
    `start`/`end` date pair (YYYY-MM-DD) for any range up to a year."""
    today = datetime.now().astimezone().date()

    start_date = end_date = None
    if start:
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            raise HTTPException(400, "start must be YYYY-MM-DD")
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(400, "end must be YYYY-MM-DD")

    if start_date or end_date:
        end_date = end_date or today
        start_date = start_date or end_date
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        span = (end_date - start_date).days + 1
        if span > 366:
            raise HTTPException(400, "range cannot exceed a year")
    else:
        days = max(1, min(days, 31))
        end_date = today
        start_date = today - timedelta(days=days - 1)
        span = days

    buckets: Dict[str, Dict[str, Any]] = {}
    for offset in range(span):
        day = start_date + timedelta(days=offset)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "label": day.strftime("%a"),
            "sessions": 0, "energyWh": 0.0, "cost": 0.0,
            "durationSeconds": 0, "peakW": None,
        }

    for entry in HISTORY:
        # Date a session by when it STARTED, so an overnight charge counts on the
        # day it began rather than jumping to the next date at midnight.
        stamp = entry.get("startedAt") or entry.get("stoppedAt")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        bucket = buckets.get(when.date().isoformat())
        if bucket is None:
            continue
        bucket["sessions"] += 1
        bucket["energyWh"] += entry.get("energyWh") or 0
        bucket["cost"] += entry.get("cost") or 0
        bucket["durationSeconds"] += entry.get("durationSeconds") or 0
        if entry.get("peakW") is not None:
            bucket["peakW"] = max(bucket["peakW"] or 0, entry["peakW"])

    daily = list(buckets.values())
    for bucket in daily:
        bucket["energyWh"] = round(bucket["energyWh"], 1)
        bucket["cost"] = round(bucket["cost"], 2)

    # A session in progress is not in HISTORY yet. Only fold it into the totals
    # when today falls inside the requested range.
    live = {"energyWh": 0.0, "cost": 0.0, "durationSeconds": 0, "powerW": None}
    if start_date <= today <= end_date:
        for cp in CHARGERS.values():
            for conn in cp.connectors.values():
                session = conn.get("session")
                if not session:
                    continue
                live["energyWh"] += session.get("energyWh") or 0
                live["durationSeconds"] += session.get("elapsedSeconds") or 0
                live["cost"] += (session.get("energyWh") or 0) / 1000 * float(
                    SETTINGS.get("pricePerKwh") or 0)
                if session.get("powerW") is not None:
                    live["powerW"] = max(live["powerW"] or 0, session["powerW"])

    totals = {
        "days": span,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "sessions": sum(b["sessions"] for b in daily),
        "energyWh": round(sum(b["energyWh"] for b in daily) + live["energyWh"], 1),
        "cost": round(sum(b["cost"] for b in daily) + live["cost"], 2),
        "durationSeconds": sum(b["durationSeconds"] for b in daily) + live["durationSeconds"],
        "peakW": max([b["peakW"] for b in daily if b["peakW"] is not None]
                     + ([live["powerW"]] if live["powerW"] is not None else []), default=None),
        "liveW": live["powerW"],
        "currency": SETTINGS.get("currency", "R"),
    }
    return {"daily": daily, "totals": totals}


@router.get("/sessions")
async def sessions(limit: int = 50, offset: int = 0,
                   start: Optional[str] = None, end: Optional[str] = None):
    """Every recorded session, newest first, paginated. Optionally filtered to
    a date range so the dashboard can show anything beyond the last 7 days."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    start_date = end_date = None
    if start:
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            raise HTTPException(400, "start must be YYYY-MM-DD")
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(400, "end must be YYYY-MM-DD")

    def in_range(entry: Dict[str, Any]) -> bool:
        if not (start_date or end_date):
            return True
        stamp = entry.get("startedAt") or entry.get("stoppedAt")
        if not stamp:
            return False
        try:
            day = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().date()
        except ValueError:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    matched = [e for e in HISTORY if in_range(e)]
    matched.sort(key=lambda e: str(e.get("startedAt") or e.get("stoppedAt") or ""),
                 reverse=True)
    page = matched[offset:offset + limit]
    return {
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "currency": SETTINGS.get("currency", "R"),
        "sessions": page,
    }


@router.get("/backup")
async def backup():
    """Everything worth keeping, as one JSON file you can save anywhere."""
    return {
        "exportedAt": utcnow(),
        "build": BUILD,
        "settings": SETTINGS,
        "sessions": HISTORY,
    }


@router.get("/solar")
async def solar_status():
    """Live inverter snapshot for the dashboard panel, plus whether the
    follow-the-charger automation is configured and enabled."""
    if not SA_ENABLED:
        return {"configured": False}
    snapshot = await SA_CLIENT.status()
    inv = snapshot or {}
    # What the automation would choose right now, for the dashboard to explain.
    would_share = None
    if snapshot:
        pv = inv.get("pvW") or 0
        soc = inv.get("batterySoc")
        would_share = (pv >= float(SETTINGS.get("solarMinPvW") or 0)
                       and (soc is None or soc >= float(SETTINGS.get("solarMinBatterySoc") or 0)))
    return {
        "configured": True,
        "auto": SA_AUTO["enabled"],
        "modes": SA_WORK_MODES,
        "modeCharging": SA_MODE_CHARGING,
        "modeIdle": SA_MODE_IDLE,
        "activeMode": inv.get("workMode"),
        "charging": _any_active_charging(),
        "reachable": snapshot is not None,
        "minPvW": SETTINGS.get("solarMinPvW"),
        "minBatterySoc": SETTINGS.get("solarMinBatterySoc"),
        "wouldShare": would_share,
        "inverter": inv,
    }


class SolarControl(BaseModel):
    # Turn the automatic follow-the-charger behaviour on or off.
    auto: Optional[bool] = None
    # Force a specific work mode by hand. Must be one of SA_WORK_MODES.
    workMode: Optional[str] = None
    # Whether a manual work-mode change should also pause the automation so it
    # isn't immediately overridden. Defaults True (manual means manual).
    pauseAuto: bool = True
    # Solar-aware charging thresholds.
    minPvW: Optional[float] = Field(default=None, ge=0, le=100000)
    minBatterySoc: Optional[float] = Field(default=None, ge=0, le=100)


@router.post("/solar")
async def solar_control(body: SolarControl):
    if not SA_ENABLED:
        raise HTTPException(400, "Solar Assistant is not configured")
    changed = {}
    if body.minPvW is not None:
        SETTINGS["solarMinPvW"] = body.minPvW
        changed["minPvW"] = body.minPvW
    if body.minBatterySoc is not None:
        SETTINGS["solarMinBatterySoc"] = body.minBatterySoc
        changed["minBatterySoc"] = body.minBatterySoc
    if body.minPvW is not None or body.minBatterySoc is not None:
        save_settings()
        # Re-evaluate immediately with the new thresholds if charging.
        if SA_AUTO["enabled"] and _any_active_charging():
            await sync_inverter_to_charging(True)
    if body.auto is not None:
        SA_AUTO["enabled"] = bool(body.auto)
        changed["auto"] = SA_AUTO["enabled"]
        # When re-enabling, immediately bring the inverter in line with reality.
        if SA_AUTO["enabled"]:
            await sync_inverter_to_charging(_any_active_charging())
    if body.workMode is not None:
        if body.workMode not in SA_WORK_MODES:
            raise HTTPException(422, f"workMode must be one of {SA_WORK_MODES}")
        # Record the manual override so the solar watcher gives it a grace period
        # before resuming automatic control.
        SA_MANUAL_OVERRIDE_AT[0] = time.time()
        # A manual mode change while the automation is on would be undone at the
        # next charging event. So a manual set also turns auto off, and says so,
        # rather than silently fighting the user.
        if SA_AUTO["enabled"] and body.pauseAuto:
            SA_AUTO["enabled"] = False
            changed["auto"] = False
        ok = await SA_CLIENT.set_work_mode(body.workMode)
        changed["workMode"] = body.workMode if ok else "unchanged"
    return {"ok": True, "changed": changed}


class RestoreBody(BaseModel):
    sessions: List[Dict[str, Any]] = []
    settings: Optional[Dict[str, Any]] = None
    replace: bool = False        # False merges, True discards what is there


@router.post("/restore")
async def restore(body: RestoreBody):
    """Load a backup or an external export back in. A session is considered the
    same if it shares a transaction id and start time, OR if it starts in the
    same minute with the same energy — so a session imported from the Teltonika
    app is recognised as the one already recorded live, despite different ids."""
    if body.replace:
        HISTORY.clear()

    def identity_keys(entry: Dict[str, Any]) -> tuple:
        txn = (entry.get("transactionId"), entry.get("startedAt"))
        stamp = entry.get("startedAt")
        minute = None
        if stamp:
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                minute = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                minute = None
        # Round energy to 0.1 kWh so tiny rounding differences still match.
        energy = round((entry.get("energyWh") or 0) / 100) * 100
        fuzzy = (minute, energy) if minute is not None else None
        return txn, fuzzy

    seen_exact = set()
    seen_fuzzy = set()
    for existing in HISTORY:
        txn, fuzzy = identity_keys(existing)
        seen_exact.add(txn)
        if fuzzy:
            seen_fuzzy.add(fuzzy)

    added = 0
    for entry in body.sessions:
        if not isinstance(entry, dict):
            continue
        txn, fuzzy = identity_keys(entry)
        if txn in seen_exact or (fuzzy and fuzzy in seen_fuzzy):
            continue
        seen_exact.add(txn)
        if fuzzy:
            seen_fuzzy.add(fuzzy)
        HISTORY.append(entry)
        added += 1

    HISTORY.sort(key=lambda e: str(e.get("startedAt") or ""))
    del HISTORY[:-HISTORY_MAX]
    save_history()

    if body.settings:
        for key in ("pricePerKwh", "currency", "batteryKwh", "targetSoc",
                    "startSoc", "chargingEfficiency", "schedule", "autoStart",
                    "autoStartIdTag"):
            if key in body.settings:
                SETTINGS[key] = body.settings[key]
        save_settings()

    log.info("restored %d sessions (%d total)", added, len(HISTORY))
    return {"added": added, "total": len(HISTORY), "settingsRestored": bool(body.settings)}


@router.get("/chargers/{charge_point_id}/measurands")
async def measurands(charge_point_id: str):
    """Which measurands this charger has actually sent — useful for solar."""
    cp = get_cp(charge_point_id)
    return {"seen": sorted(cp.measurands_seen), "vendorData": cp.vendor_data[-5:]}


@router.post("/chargers/{charge_point_id}/trigger")
async def trigger_message(charge_point_id: str, body: TriggerBody):
    kwargs: Dict[str, Any] = {"requested_message": body.requestedMessage}
    if body.connectorId is not None:
        kwargs["connector_id"] = body.connectorId
    return await send(get_cp(charge_point_id), "TriggerMessage", **kwargs)


api.include_router(router)


# Browsers cache HTML aggressively, which makes a redeploy look like it did
# nothing. These pages are small; always serve them fresh.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@api.get("/")
async def dashboard(request: Request):
    if not valid_session(request):
        return RedirectResponse("/login", status_code=302)
    return FileResponse("static/index.html", headers=NO_CACHE)


@api.get("/login")
async def login_page(request: Request):
    if valid_session(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse("static/login.html", headers=NO_CACHE)


@api.get("/favicon.svg")
async def favicon():
    return FileResponse("static/favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------
# Home screen app assets. Served individually rather than by mounting the
# static directory, which would expose the page HTML without a session.
# --------------------------------------------------------------------------
HOME_SCREEN_ASSETS = {
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon.svg": "image/svg+xml",
}


@api.get("/static-asset/{name}")
async def static_asset(name: str):
    media_type = HOME_SCREEN_ASSETS.get(name)
    if not media_type:
        raise HTTPException(404, "Not found")
    return FileResponse(f"static/{name}", media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"})


@api.get("/apple-touch-icon.png")
@api.get("/apple-touch-icon-precomposed.png")
async def apple_icon():
    """iOS requests this path directly when adding to the home screen."""
    return FileResponse("static/apple-touch-icon.png", media_type="image/png")


@api.get("/certificate")
async def download_certificate():
    """The self-signed certificate, so it can be installed on a device."""
    if not os.path.isfile(TLS_CERT):
        raise HTTPException(404, "No certificate in use")
    return FileResponse(TLS_CERT, media_type="application/x-x509-ca-cert",
                        filename="jtech-grid-control.crt")


@api.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("static/manifest.webmanifest",
                        media_type="application/manifest+json")


@api.get("/version")
async def version():
    """Quick way to confirm which build is actually running."""
    return {"build": BUILD, "hasLogin": os.path.exists("static/login.html"),
            "persistent": PERSISTENT, "tls": TLS_ENABLED}


# --------------------------------------------------------------------------
# Auto-start watcher
# --------------------------------------------------------------------------
WAITING_STATES = ("Preparing", "SuspendedEVSE", "SuspendedEV")
_autostart_attempts: Dict[str, float] = {}
AUTOSTART_RETRY = 120        # seconds between attempts for the same connector
AUTOSTART_REJECT_BACKOFF = 1800   # after a Rejected start, wait this long before retrying
_autostart_backoff: Dict[str, float] = {}   # per-connector current retry interval
_autostart_probe: Dict[str, float] = {}   # last status-probe time per connector
_finishing_since: Dict[str, float] = {}   # when a connector entered Finishing


def _local_now() -> datetime:
    """Current time in the configured timezone. Resolves TZ explicitly via
    zoneinfo rather than trusting the container's ambient clock — some container
    setups don't apply TZ to the process clock, which would put schedule checks
    hours off (e.g. a charge stopped as 'window closed' while actually inside
    the window because the clock was on UTC)."""
    tz_name = os.environ.get("TZ")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 - bad/missing tz db, fall back
            pass
    return datetime.now().astimezone()


def in_schedule_window(now: Optional[datetime] = None) -> bool:
    """True if the current local time falls inside the configured window."""
    schedule = SETTINGS.get("schedule")
    if not schedule:
        return True                     # no window set means always allowed
    now = now or _local_now()
    minutes = now.hour * 60 + now.minute

    def to_minutes(text: str) -> int:
        hours, mins = text.split(":")
        return int(hours) * 60 + int(mins)

    start, end = to_minutes(schedule["start"]), to_minutes(schedule["end"])
    if start == end:
        return True
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


async def solar_watcher() -> None:
    """While a car is charging, re-evaluate the inverter mode against live solar
    every 30 seconds, so the mode follows the sun rather than being fixed at
    plug-in. When the sun fades or the battery drops below the floor it reverts
    to protecting the battery; when they recover it shares to the board again.
    set_work_mode is a no-op when the mode is already correct, so a stable sky
    means no writes."""
    while True:
        await asyncio.sleep(30)
        if not SA_CLIENT or not SA_AUTO["enabled"]:
            continue
        # Respect a recent manual override in either state.
        if time.time() - SA_MANUAL_OVERRIDE_AT[0] < SA_MANUAL_GRACE_SECONDS:
            continue
        charging = _any_active_charging()
        if not charging:
            # Nothing is charging. The inverter should be in idle mode (share to
            # the whole board). If it drifted to the charging mode — e.g. a
            # charger dropped mid-session and left it stuck — correct it. This is
            # the safety net for the overnight "stuck on load" failure.
            try:
                await sync_inverter_to_charging(False)
            except Exception as exc:  # noqa: BLE001
                log.warning("solar watcher idle reconcile: %s", exc)
            continue
        try:
            await sync_inverter_to_charging(True)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("solar watcher: %s", exc)


async def autostart_watcher() -> None:
    """Start a session when a cable is plugged in, and stop it when the
    schedule window closes. Runs every 20 seconds."""
    while True:
        await asyncio.sleep(20)
        mode = SETTINGS.get("autoStart", "off")
        if mode == "off":
            continue
        inside = in_schedule_window()

        for cp in list(CHARGERS.values()):
            for connector_id, conn in list(cp.connectors.items()):
                if connector_id == 0:
                    continue
                key = f"{cp.id}:{connector_id}"
                running = conn.get("transactionId")

                # Window closed while charging: stop what we started, and record
                # the session at the window boundary. We both ask the charger to
                # stop AND finalise locally, so the recorded end is the scheduled
                # time even if the charger is slow to send its StopTransaction.
                if running and mode == "schedule" and not inside:
                    log.info("autostart: window closed, stopping %s", key)
                    cp._finalise_session(connector_id, utcnow(),
                                         reason="ScheduleWindowClosed")
                    try:
                        await cp.call(request("RemoteStopTransaction", transaction_id=running))
                        record(cp.id, "AutoStop", {"connectorId": connector_id})
                    except (asyncio.TimeoutError, OSError) as exc:
                        log.warning("autostart stop failed: %s", exc)
                    continue

                if running or conn.get("status") not in WAITING_STATES:
                    # Inside the schedule window with the charger connected but in
                    # a state we can't start from — "Available"/no status (a
                    # reconnect at the window boundary) or "Finishing" (a session
                    # just ended and the charger hasn't returned to "Preparing"
                    # yet). Nudge it to re-report status so it moves on and the
                    # next pass can start. Without this, stopping a charge and
                    # leaving the cable in can strand the connector in "Finishing"
                    # and silently miss the rest of the window.
                    PROBE_STATES = (None, "Available", "Finishing", "SuspendedEVSE")
                    if (mode == "schedule" and inside and not running
                            and conn.get("status") in PROBE_STATES):
                        # "Finishing" means a session just ended with the cable
                        # still in — a car is present. If it lingers there (stuck,
                        # not just transiting), a RemoteStart will move it into a
                        # fresh session. Give it one probe first, then start.
                        if conn.get("status") == "Finishing":
                            first_seen = _finishing_since.setdefault(key, time.time())
                            if time.time() - first_seen >= 20:
                                # lingered — fall through to the start attempt
                                pass
                            else:
                                last_probe = _autostart_probe.get(key, 0)
                                if time.time() - last_probe >= 20:
                                    _autostart_probe[key] = time.time()
                                    try:
                                        await cp.call(request(
                                            "TriggerMessage",
                                            requested_message="StatusNotification"))
                                    except Exception as exc:  # noqa: BLE001
                                        log.debug("autostart probe failed: %s", exc)
                                continue
                        else:
                            last_probe = _autostart_probe.get(key, 0)
                            if time.time() - last_probe >= 20:
                                _autostart_probe[key] = time.time()
                                log.info("autostart: window open but %s is %r — "
                                         "requesting fresh status", key, conn.get("status"))
                                try:
                                    await cp.call(request(
                                        "TriggerMessage",
                                        requested_message="StatusNotification"))
                                except Exception as exc:  # noqa: BLE001
                                    log.debug("autostart probe failed: %s", exc)
                            continue
                    else:
                        _autostart_attempts.pop(key, None)
                        _finishing_since.pop(key, None)
                        continue
                else:
                    _finishing_since.pop(key, None)
                if mode == "schedule" and not inside:
                    continue
                last = _autostart_attempts.get(key, 0)
                wait = _autostart_backoff.get(key, AUTOSTART_RETRY)
                if time.time() - last < wait:
                    continue

                _autostart_attempts[key] = time.time()
                log.info("autostart: starting %s (status %s)", key, conn.get("status"))
                try:
                    response = await cp.call(request(
                        "RemoteStartTransaction",
                        id_tag=SETTINGS.get("autoStartIdTag", "AUTO"),
                        connector_id=connector_id))
                    status = getattr(response, "status", None)
                    record(cp.id, "AutoStart",
                           {"connectorId": connector_id, "status": status})
                    if status == "Rejected":
                        # The charger won't start — almost always because the car
                        # is already at target and sitting in SuspendedEV (a
                        # completed session left plugged in). Retrying every couple
                        # of minutes just spams the log and the charger, so back
                        # off. It still recovers if the situation changes (battery
                        # drops and the car accepts again), just on a slow cadence.
                        _autostart_backoff[key] = AUTOSTART_REJECT_BACKOFF
                    else:
                        _autostart_backoff.pop(key, None)
                except (asyncio.TimeoutError, OSError) as exc:
                    log.warning("autostart failed: %s", exc)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def generate_self_signed(cert_path: str, key_path: str, names: List[str]) -> bool:
    """Write a self-signed certificate covering every name and IP given."""
    try:
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log.warning("cryptography is not installed, cannot generate a certificate")
        return False

    alt: List[Any] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    if not alt:
        alt = [x509.DNSName("localhost")]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JTech Grid Control"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            # 825 days is the maximum iOS and Safari will accept
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    try:
        os.makedirs(os.path.dirname(cert_path), exist_ok=True)
        with open(cert_path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
        os.chmod(key_path, 0o600)
    except OSError as exc:
        log.warning("Could not write the certificate to %s (%s)", cert_path, exc)
        return False

    log.info("Generated a self-signed certificate for %s", ", ".join(names))
    log.info("Valid until %s. Download it from /certificate to install on your "
             "devices and remove the browser warning.",
             (now + timedelta(days=825)).strftime("%d %b %Y"))
    return True


def prepare_tls() -> None:
    """Decide whether we can serve HTTPS, generating a certificate if needed."""
    global TLS_ENABLED, COOKIE_SECURE
    if os.path.isfile(TLS_CERT) and os.path.isfile(TLS_KEY):
        TLS_ENABLED = True
    elif AUTO_CERT:
        names = sorted(TRUSTED_ORIGINS) or []
        names = [n.rsplit(":", 1)[0] if ":" in n else n for n in names]
        if "localhost" not in names:
            names.append("localhost")
        TLS_ENABLED = generate_self_signed(TLS_CERT, TLS_KEY, names)

    # A Secure cookie over plain HTTP is discarded by the browser, which looks
    # exactly like a wrong password. Never let that combination happen.
    if COOKIE_SECURE and not TLS_ENABLED and not TRUST_PROXY:
        log.warning("=" * 70)
        log.warning("OCPP_COOKIE_SECURE is 1 but this server is not serving HTTPS")
        log.warning("and no reverse proxy is trusted. The browser would discard")
        log.warning("the session cookie and sign-in would loop forever.")
        log.warning("Falling back to a non-Secure cookie so you can sign in.")
        log.warning("Fix the certificate, or set OCPP_COOKIE_SECURE=0.")
        log.warning("=" * 70)
        COOKIE_SECURE = False


def prepare_credentials() -> None:
    global ADMIN_PASSWORD
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(12)
        log.warning("=" * 64)
        log.warning("No OCPP_ADMIN_PASSWORD set. Generated one for this run only:")
        log.warning("    user: %s", ADMIN_USER)
        log.warning("    pass: %s", ADMIN_PASSWORD)
        log.warning("Set OCPP_ADMIN_PASSWORD in the environment to keep it.")
        log.warning("=" * 64)
    elif len(ADMIN_PASSWORD) < 12:
        log.warning("OCPP_ADMIN_PASSWORD is short. Use 16+ characters if this is "
                    "reachable from the internet.")
    now = datetime.now().astimezone()
    wanted = os.environ.get("TZ")
    if wanted and now.tzname() in ("UTC", "GMT") and wanted not in ("UTC", "Etc/UTC"):
        log.warning("TZ is set to %s but the clock is running on %s. The zoneinfo "
                    "database is missing, so schedules will be wrong by the local "
                    "offset. Install tzdata in the image.", wanted, now.tzname())
    else:
        log.info("Local time %s (%s) — schedules use this clock",
                 now.strftime("%H:%M"), now.tzname())
    if not COOKIE_SECURE:
        log.info("Session cookie is not marked Secure. Set OCPP_COOKIE_SECURE=1 "
                 "once you are behind HTTPS.")


async def main(host: str, ocpp_port: int, api_port: int) -> None:
    prepare_credentials()
    check_persistence()
    migrate_legacy_data()
    prepare_tls()
    load_settings()
    load_history()
    ws_server = await websockets.serve(
        on_connect, host, ocpp_port, subprotocols=["ocpp1.6"], ping_interval=30, ping_timeout=30
    )
    log.info("JTech Grid Control  build %s", BUILD)
    log.info("OCPP endpoint       ws://%s:%s/<ChargePointId>", host, ocpp_port)
    log.info("Dashboard           %s://%s:%s/",
             "https" if TLS_ENABLED else "http", host, api_port)
    if TLS_ENABLED and not COOKIE_SECURE:
        log.warning("TLS is on but OCPP_COOKIE_SECURE is 0. Set it to 1 so the "
                    "session cookie is never sent unencrypted.")
    if not TLS_ENABLED and COOKIE_SECURE and not TRUST_PROXY:
        log.warning("OCPP_COOKIE_SECURE is 1, no certificate at %s, and "
                    "OCPP_TRUST_PROXY is 0. If nothing terminates TLS in front "
                    "of this, browsers will drop the session cookie and sign-in "
                    "will loop.", TLS_CERT)
    if TRUST_PROXY:
        log.info("Trusting X-Forwarded-* headers. Only correct if a reverse "
                 "proxy is the sole route to port %s.", api_port)
        if TRUSTED_ORIGINS:
            log.info("Accepting requests from: %s", ", ".join(sorted(TRUSTED_ORIGINS)))
        else:
            log.warning("OCPP_TRUSTED_ORIGINS is empty. If the proxy rewrites the "
                        "Host header, control actions will be refused as "
                        "cross-origin.")
    if not TLS_ENABLED:
        log.info("No certificate at %s — serving plain HTTP", TLS_CERT)

    watcher = asyncio.create_task(autostart_watcher())
    solar_task = asyncio.create_task(solar_watcher()) if SA_ENABLED else None

    if SA_ENABLED:
        log.info("Solar Assistant at %s: work mode will follow charging "
                 "(%s while charging, %s idle)", SA_HOST, SA_MODE_CHARGING, SA_MODE_IDLE)
        # Self-correct on startup: if nothing is charging, make sure the inverter
        # is in idle mode, so a restart mid-idle never leaves it stuck.
        async def _sa_startup():
            await asyncio.sleep(3)
            if not _any_active_charging():
                await sync_inverter_to_charging(False)
        asyncio.create_task(_sa_startup())

    tls_options = {}
    if TLS_ENABLED:
        tls_options = {"ssl_certfile": TLS_CERT, "ssl_keyfile": TLS_KEY}

    config = uvicorn.Config(api, host=host, port=api_port, log_level="warning",
                            **tls_options)
    await uvicorn.Server(config).serve()
    watcher.cancel()
    ws_server.close()
    await ws_server.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--ocpp-port", type=int, default=9000)
    parser.add_argument("--api-port", type=int, default=8080)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.host, args.ocpp_port, args.api_port))
    except KeyboardInterrupt:
        log.info("Stopped")
