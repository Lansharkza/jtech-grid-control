"""
A minimal OCPP 1.6J charge point, for testing the central system without the
EVC121 on the bench.

    python simulator.py --url ws://localhost:9000 --id SIM001
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s  SIM  %(message)s")
log = logging.getLogger("sim")


def _resolve(module, name):
    for candidate in (name, name + "Payload"):
        if hasattr(module, candidate):
            return getattr(module, candidate)
    raise AttributeError(name)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Simulator(BaseChargePoint):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.transaction_id = None
        self.meter_wh = 12345
        self.config = {"Solar": "0", "SolarCharging": "regular"}

    plugged_in = False

    async def boot(self):
        await self.call(_resolve(call, "BootNotification")(
            charge_point_vendor="Teltonika", charge_point_model="TeltoCharge EVC121",
            firmware_version="1.0.0-sim", charge_point_serial_number="SIM-0001"))
        await self.status(1, "Preparing" if self.plugged_in else "Available")

    async def status(self, connector_id, status, error_code="NoError"):
        await self.call(_resolve(call, "StatusNotification")(
            connector_id=connector_id, error_code=error_code, status=status, timestamp=utcnow()))

    async def loop(self):
        while True:
            await self.call(_resolve(call, "Heartbeat")())
            if self.transaction_id:
                self.meter_wh += 180
                await self.call(_resolve(call, "MeterValues")(
                    connector_id=1, transaction_id=self.transaction_id,
                    meter_value=[{"timestamp": utcnow(), "sampledValue": [
                        {"value": str(self.meter_wh), "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                        {"value": "11000", "measurand": "Power.Active.Import", "unit": "W"},
                        {"value": "16", "measurand": "Current.Import", "unit": "A"},
                        {"value": "230", "measurand": "Voltage", "unit": "V"},
                        {"value": "34", "measurand": "Temperature", "unit": "Celsius"},
                        {"value": "4200", "measurand": "Power.Active.Export", "unit": "W"},
                    ]}]))
            await asyncio.sleep(10)

    @on("RemoteStartTransaction")
    async def on_remote_start(self, id_tag, **kwargs):
        asyncio.create_task(self._start(id_tag))
        return _resolve(call_result, "RemoteStartTransaction")(status="Accepted")

    async def _start(self, id_tag):
        await asyncio.sleep(1)
        await self.status(1, "Preparing")
        response = await self.call(_resolve(call, "StartTransaction")(
            connector_id=1, id_tag=id_tag, meter_start=self.meter_wh, timestamp=utcnow()))
        self.transaction_id = response.transaction_id
        await self.status(1, "Charging")
        log.info("transaction %s started", self.transaction_id)

    @on("RemoteStopTransaction")
    async def on_remote_stop(self, transaction_id, **kwargs):
        asyncio.create_task(self._stop(transaction_id))
        return _resolve(call_result, "RemoteStopTransaction")(status="Accepted")

    async def _stop(self, transaction_id):
        await asyncio.sleep(1)
        await self.call(_resolve(call, "StopTransaction")(
            meter_stop=self.meter_wh, timestamp=utcnow(),
            transaction_id=transaction_id, reason="Remote"))
        self.transaction_id = None
        await self.status(1, "Available")

    @on("GetConfiguration")
    async def on_get_configuration(self, **kwargs):
        return _resolve(call_result, "GetConfiguration")(configuration_key=[
            {"key": "Solar", "readonly": False, "value": self.config["Solar"]},
            {"key": "SolarCharging", "readonly": False, "value": self.config["SolarCharging"]},
            {"key": "HeartbeatInterval", "readonly": False, "value": "60"},
            {"key": "MeterValueSampleInterval", "readonly": False, "value": "10"},
            {"key": "NumberOfConnectors", "readonly": True, "value": "1"},
            {"key": "AuthorizeRemoteTxRequests", "readonly": False, "value": "false"},
        ])

    @on("ChangeConfiguration")
    async def on_change_configuration(self, key, value, **kwargs):
        log.info("config %s = %s", key, value)
        if key in self.config:
            self.config[key] = value
        return _resolve(call_result, "ChangeConfiguration")(status="Accepted")

    @on("SetChargingProfile")
    async def on_set_charging_profile(self, **kwargs):
        return _resolve(call_result, "SetChargingProfile")(status="Accepted")

    @on("ClearChargingProfile")
    async def on_clear_charging_profile(self, **kwargs):
        return _resolve(call_result, "ClearChargingProfile")(status="Accepted")

    @on("ChangeAvailability")
    async def on_change_availability(self, **kwargs):
        return _resolve(call_result, "ChangeAvailability")(status="Accepted")

    @on("UnlockConnector")
    async def on_unlock(self, **kwargs):
        return _resolve(call_result, "UnlockConnector")(status="Unlocked")

    @on("Reset")
    async def on_reset(self, **kwargs):
        return _resolve(call_result, "Reset")(status="Accepted")

    @on("TriggerMessage")
    async def on_trigger(self, requested_message, **kwargs):
        return _resolve(call_result, "TriggerMessage")(status="Accepted")

    @on("ClearCache")
    async def on_clear_cache(self, **kwargs):
        return _resolve(call_result, "ClearCache")(status="Accepted")

    @on("GetCompositeSchedule")
    async def on_get_composite(self, connector_id, duration, **kwargs):
        return _resolve(call_result, "GetCompositeSchedule")(status="Accepted")

    @on("ReserveNow")
    async def on_reserve(self, **kwargs):
        return _resolve(call_result, "ReserveNow")(status="Accepted")

    @on("CancelReservation")
    async def on_cancel_reservation(self, **kwargs):
        return _resolve(call_result, "CancelReservation")(status="Accepted")

    @on("GetLocalListVersion")
    async def on_local_list_version(self, **kwargs):
        return _resolve(call_result, "GetLocalListVersion")(list_version=1)

    @on("SendLocalList")
    async def on_send_local_list(self, **kwargs):
        return _resolve(call_result, "SendLocalList")(status="Accepted")

    @on("GetDiagnostics")
    async def on_get_diagnostics(self, **kwargs):
        return _resolve(call_result, "GetDiagnostics")(file_name="diag.tar.gz")

    @on("UpdateFirmware")
    async def on_update_firmware(self, **kwargs):
        return _resolve(call_result, "UpdateFirmware")()


async def main(url, cp_id, plugged=False):
    async with websockets.connect(f"{url}/{cp_id}", subprotocols=["ocpp1.6"]) as ws:
        cp = Simulator(cp_id, ws)
        cp.plugged_in = plugged
        await asyncio.gather(cp.start(), cp.boot(), cp.loop())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://localhost:9000")
    parser.add_argument("--id", default="SIM001")
    parser.add_argument("--plugged", action="store_true",
                        help="report Preparing, as though a cable is in the car")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.id, args.plugged))
