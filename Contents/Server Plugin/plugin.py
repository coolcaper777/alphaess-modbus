try:
    import indigo
except ImportError:
    pass

import json
import logging
import time
from typing import Optional

# pymodbus is declared in requirements.txt and installed automatically by
# Indigo on first launch (same mechanism Automate Pulse 2 uses for aiopulse2) -
# no manual sys.path/vendoring needed.
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# AlphaESS inverters only ever answer on Modbus unit/slave (pymodbus calls it
# "device_id") 0x55 (85), not the Modbus default of 1 - every third-party
# AlphaESS client (SorX14/alphaess_modbus, ha-alphaess-modbus) hardcodes this
# because it isn't configurable on the inverter itself. Still exposed as a
# device ConfigUI field rather than hardcoded, in case a future model differs.
DEFAULT_UNIT_ID = 85

# Register addresses/types pulled from SorX14/alphaess_modbus (MIT-licensed)
# registers.json, cross-checked against AlphaESS's own dispatch register
# addresses documented on https://projects.hillviewlodge.ie/alphaess/. All are
# read via Modbus function code 3 (Read Holding Registers). "words": 2 means a
# 32-bit value spanning two consecutive registers, high word first.
#
# pvPower is deliberately NOT read from the single "total_active_power_pv_meter"
# register (address 161) - that's an optional external PV metering CT accessory,
# and reads a flat 0 on installations (like this one) that don't have it wired
# up, even with real solar production happening. The real source is
# PV_STRING_POWER_ADDRESSES below, summed.
REGISTERS = {
    "gridPower": {"address": 33, "words": 2, "signed": True, "decimals": 0},
    "batteryPower": {"address": 294, "words": 1, "signed": True, "decimals": 0},
    "batterySoC": {"address": 258, "words": 1, "signed": False, "decimals": 1},
    "batterySoH": {"address": 283, "words": 1, "signed": False, "decimals": 1},
    "lifetimeFeedToGrid": {"address": 16, "words": 2, "signed": False, "decimals": 2},
    "lifetimeConsumedFromGrid": {"address": 18, "words": 2, "signed": False, "decimals": 2},
}

# The inverter's own per-MPPT-string PV readings: voltage, current, power (2
# words) per string, 4 registers each, 6 strings back to back with no gaps -
# unused strings simply read 0. registers.json lists pv3_power's type as a
# single 16-bit "register", but the surrounding address spacing (pv4_voltage
# starts 2 registers after pv3_power, same as every other string) shows it's
# actually 2 words like the rest - treated as such here.
INVERTER_PV_BLOCK_START = 1053  # pv1_voltage
INVERTER_PV_BLOCK_COUNT = 24    # through pv6_power inclusive (6 strings x 4 registers)
PV_STRING_POWER_ADDRESSES = [1055, 1059, 1063, 1067, 1071, 1075]

# Batched read ranges covering every address in REGISTERS plus the PV string
# block above, so a poll takes 3 Modbus round trips instead of 12+. Each tuple
# is (start_address, count).
REGISTER_CLUSTERS = [
    (16, 19),   # covers lifetimeFeedToGrid (16-17), lifetimeConsumedFromGrid (18-19), gridPower (33-34)
    (INVERTER_PV_BLOCK_START, INVERTER_PV_BLOCK_COUNT),
    (258, 37),  # covers batterySoC (258), batterySoH (283), batteryPower (294)
]

# Served at http://<this-mac>:8176/message/com.coolcaper.alphaessmodbus/dashboard
# by the `dashboard`/`dashboard_data` methods below, via Indigo's built-in
# plugin HTTP responder. Self-contained (no CDN, no build step) so it works
# on any browser on the LAN with nothing installed. Colors are the validated
# categorical palette (see the indigo skill's dataviz reference) - fixed
# identity per node (grid=blue, solar=orange, battery=aqua, home=yellow),
# never re-ordered/cycled; direction (import/export, charge/discharge) is
# conveyed with an arrow + text label rather than another color, since that's
# polarity, not identity.
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AlphaESS</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page-plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --border: rgba(11,11,11,0.10);
    --series-grid: #2a78d6;
    --series-solar: #eb6834;
    --series-battery: #1baf7a;
    --series-home: #eda100;
    --status-critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page-plane: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --border: rgba(255,255,255,0.10);
      --series-grid: #3987e5;
      --series-solar: #d95926;
      --series-battery: #199e70;
      --series-home: #c98500;
      --status-critical: #e66767;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page-plane);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 48px;
  }
  .page { max-width: 880px; margin: 0 auto; }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; }
  .meta { color: var(--text-muted); font-size: 13px; }
  select {
    font: inherit; color: var(--text-primary); background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px;
  }
  .banner {
    display: none; align-items: center; gap: 8px; margin-bottom: 16px;
    padding: 10px 14px; border-radius: 10px; font-size: 14px;
    color: var(--status-critical); border: 1px solid var(--status-critical);
    background: color-mix(in srgb, var(--status-critical) 10%, transparent);
  }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  .tile-label { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; margin-bottom: 10px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .dot-grid { background: var(--series-grid); }
  .dot-solar { background: var(--series-solar); }
  .dot-battery { background: var(--series-battery); }
  .dot-home { background: var(--series-home); }
  .value { font-size: 28px; font-weight: 600; line-height: 1.1; }
  .sub { color: var(--text-muted); font-size: 13px; margin-top: 6px; }
  footer { margin-top: 24px; color: var(--text-muted); font-size: 12px; }
</style>
</head>
<body>
<div class="page">
  <header>
    <h1 id="deviceName">AlphaESS Inverter</h1>
    <div style="display:flex; align-items:center; gap:10px;">
      <select id="deviceSelect" style="display:none;"></select>
      <span class="meta" id="lastUpdated">-</span>
    </div>
  </header>

  <div class="banner" id="banner">&#9888; <span id="bannerText"></span></div>

  <div class="grid">
    <div class="tile">
      <div class="tile-label"><span class="dot dot-solar"></span>Solar</div>
      <div class="value" id="pvPower">-</div>
    </div>
    <div class="tile">
      <div class="tile-label"><span class="dot dot-grid"></span>Grid</div>
      <div class="value" id="gridPower">-</div>
      <div class="sub" id="gridDirection">-</div>
    </div>
    <div class="tile">
      <div class="tile-label"><span class="dot dot-battery"></span>Battery</div>
      <div class="value" id="batterySoC">-</div>
      <div class="sub" id="batteryDirection">-</div>
    </div>
    <div class="tile">
      <div class="tile-label"><span class="dot dot-home"></span>Home</div>
      <div class="value" id="loadPower">-</div>
    </div>
  </div>

  <footer>Lifetime fed to grid: <span id="lifetimeFeed">-</span> &middot; Lifetime consumed from grid: <span id="lifetimeConsumed">-</span></footer>
</div>
<script>
(function () {
  var POLL_MS = 5000;
  var selectedDeviceId = null;
  var deviceSelect = document.getElementById("deviceSelect");

  function formatPower(watts) {
    var abs = Math.abs(watts);
    if (abs < 1000) return Math.round(watts) + " W";
    return (watts / 1000).toFixed(2) + " kW";
  }

  function setBanner(message) {
    var banner = document.getElementById("banner");
    if (message) {
      document.getElementById("bannerText").textContent = message;
      banner.style.display = "flex";
    } else {
      banner.style.display = "none";
    }
  }

  function render(data) {
    document.getElementById("deviceName").textContent = data.deviceName || "AlphaESS Inverter";
    document.getElementById("lastUpdated").textContent = "Updated " + new Date().toLocaleTimeString();

    document.getElementById("pvPower").textContent = formatPower(data.pvPower);
    document.getElementById("gridPower").textContent = formatPower(Math.abs(data.gridPower));
    document.getElementById("gridDirection").textContent =
      data.gridPower < 0 ? "\u2190 Exporting to grid" : (data.gridPower > 0 ? "\u2192 Importing from grid" : "Idle");

    document.getElementById("batterySoC").textContent = Number(data.batterySoC).toFixed(1) + "%";
    var bp = data.batteryPower;
    document.getElementById("batteryDirection").textContent =
      Math.abs(bp) < 15 ? (formatPower(bp) + " \u00b7 Idle")
        : (bp > 0 ? "\u2193 Discharging \u00b7 " + formatPower(bp) : "\u2191 Charging \u00b7 " + formatPower(Math.abs(bp)));

    document.getElementById("loadPower").textContent = formatPower(data.loadPower);

    document.getElementById("lifetimeFeed").textContent = Number(data.lifetimeFeedToGrid).toFixed(2) + " kWh";
    document.getElementById("lifetimeConsumed").textContent = Number(data.lifetimeConsumedFromGrid).toFixed(2) + " kWh";

    if (data.errorState) {
      setBanner("Device reporting an error: " + data.errorState);
    } else {
      setBanner(null);
    }

    if (data.devices && data.devices.length > 1) {
      deviceSelect.style.display = "inline-block";
      if (deviceSelect.options.length !== data.devices.length) {
        deviceSelect.innerHTML = "";
        data.devices.forEach(function (d) {
          var opt = document.createElement("option");
          opt.value = d.id;
          opt.textContent = d.name;
          deviceSelect.appendChild(opt);
        });
      }
      deviceSelect.value = data.deviceId;
    }
    selectedDeviceId = data.deviceId;
  }

  function poll() {
    var url = "dashboard_data" + (selectedDeviceId ? ("?deviceId=" + selectedDeviceId) : "");
    fetch(url).then(function (res) { return res.json(); }).then(function (data) {
      if (!data.ok) {
        setBanner(data.error || "No AlphaESS Inverter device found");
        return;
      }
      render(data);
    }).catch(function () {
      setBanner("Could not reach the AlphaESS Modbus plugin");
    });
  }

  deviceSelect.addEventListener("change", function () {
    selectedDeviceId = deviceSelect.value;
    poll();
  });

  poll();
  setInterval(poll, POLL_MS);
})();
</script>
</body>
</html>
"""


def _decode_value(registers: list, signed: bool, decimals: int) -> float:
    """Combine 1 or 2 raw Modbus registers into a signed/scaled numeric value.

    Args:
        registers (list): One or two raw 16-bit register values, as returned by pymodbus.
        signed (bool): Whether to interpret the combined value as two's-complement signed.
        decimals (int): How many implied decimal places the raw integer encodes.

    Returns:
        float: The decoded value, scaled by ``10 ** decimals``.
    """
    if len(registers) == 1:
        raw = registers[0]
        if signed and raw >= 0x8000:
            raw -= 0x10000
    else:
        raw = (registers[0] << 16) | registers[1]
        if signed and raw >= 0x80000000:
            raw -= 0x100000000
    return raw / (10 ** decimals) if decimals else raw


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId: str, pluginDisplayName: str, pluginVersion: str, pluginPrefs: indigo.Dict) -> None:
        """Initialize the plugin instance and set the debug logging level.

        Args:
            pluginId (str): This plugin's bundle identifier.
            pluginDisplayName (str): The plugin's display name.
            pluginVersion (str): The plugin's version string.
            pluginPrefs (indigo.Dict): Saved plugin preferences.
        """
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = self.pluginPrefs.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self._next_poll_at: dict = {}

    def startup(self) -> None:
        """Called once by Indigo when the plugin starts running."""
        self.logger.info("AlphaESS Modbus plugin starting up...")

    def shutdown(self) -> None:
        """Called once by Indigo when the plugin is stopping."""
        self.logger.info("AlphaESS Modbus plugin shutting down...")

    def closedPrefsConfigUi(self, valuesDict: indigo.Dict, userCancelled: bool) -> None:
        """Apply the plugin preferences dialog's saved values.

        Args:
            valuesDict (indigo.Dict): The saved preference values.
            userCancelled (bool): True if the dialog was cancelled instead of saved.
        """
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

    def runConcurrentThread(self) -> None:
        """Indigo's polling loop entry point.

        Ticks every 5 seconds and polls each enabled inverter device once its
        own configured ``pollInterval`` has elapsed, so devices can be polled
        at different rates.
        """
        try:
            while True:
                now = time.time()
                for dev in indigo.devices.iter("self.inverter"):
                    if not dev.enabled or not dev.configured:
                        continue
                    if now < self._next_poll_at.get(dev.id, 0):
                        continue
                    interval = int(dev.pluginProps.get("pollInterval", 30))
                    self._next_poll_at[dev.id] = now + interval
                    try:
                        self._poll_inverter(dev)
                    except Exception:
                        self.logger.exception(f"Error polling {dev.name}")
                self.sleep(5)
        except self.StopThread:
            pass

    def deviceStartComm(self, dev: indigo.Device) -> None:
        """Start communication with a device when it's enabled/created.

        Args:
            dev (indigo.Device): The device being started.
        """
        super().deviceStartComm(dev)
        self.logger.debug(f"deviceStartComm: {dev.name}")
        dev.stateListOrDisplayStateIdChanged()
        if dev.configured:
            # dev.configured is False for the moment between "New Device" and
            # Save being clicked (pluginProps are still empty then) - polling
            # during that window just produces a misleading "no IP" error.
            try:
                self._poll_inverter(dev)
            except Exception:
                self.logger.exception(f"Error polling {dev.name}")

    def deviceStopComm(self, dev: indigo.Device) -> None:
        """Stop communication with a device when it's disabled/deleted.

        Args:
            dev (indigo.Device): The device being stopped.
        """
        super().deviceStopComm(dev)
        self.logger.debug(f"deviceStopComm: {dev.name}")
        self._next_poll_at.pop(dev.id, None)

    def _poll_inverter(self, dev: indigo.Device) -> None:
        """Read one poll's worth of Modbus registers from an inverter and update its states.

        Args:
            dev (indigo.Device): The inverter device to poll.
        """
        address = dev.pluginProps.get("address", "")
        if not address:
            dev.setErrorStateOnServer("No IP address configured")
            return
        try:
            port = int(dev.pluginProps.get("port", 502))
            unit_id = int(dev.pluginProps.get("unitId", DEFAULT_UNIT_ID))
        except (TypeError, ValueError):
            # Both fields are plain textfields in Devices.xml - Indigo doesn't
            # enforce numeric-only input, so a cleared/typo'd field would
            # otherwise raise here unguarded, before the try block below that
            # actually sets an error state on failure.
            dev.setErrorStateOnServer("Invalid Port or Unit ID - must be a number")
            self.logger.error(
                f"{dev.name}: Port/Unit ID must be numeric "
                f"(got port={dev.pluginProps.get('port')!r}, unitId={dev.pluginProps.get('unitId')!r})"
            )
            return

        client = ModbusTcpClient(address, port=port, timeout=5)
        if not client.connect():
            dev.setErrorStateOnServer("Connection failed")
            self.logger.error(f"{dev.name}: could not connect to {address}:{port}")
            return

        try:
            values = {}
            cluster_registers = {}
            for start, count in REGISTER_CLUSTERS:
                result = client.read_holding_registers(start, count=count, device_id=unit_id)
                if result.isError():
                    raise ModbusException(f"error reading registers {start}-{start + count - 1}: {result}")
                cluster_registers[start] = result.registers
                for name, spec in REGISTERS.items():
                    if start <= spec["address"] and spec["address"] + spec["words"] <= start + count:
                        offset = spec["address"] - start
                        regs = result.registers[offset: offset + spec["words"]]
                        values[name] = _decode_value(regs, spec["signed"], spec["decimals"])
                        self.logger.debug(f"{dev.name}: {name} = {values[name]} (raw {regs})")

            pv_block = cluster_registers[INVERTER_PV_BLOCK_START]
            pv_power = 0
            for addr in PV_STRING_POWER_ADDRESSES:
                offset = addr - INVERTER_PV_BLOCK_START
                pv_power += _decode_value(pv_block[offset:offset + 2], signed=False, decimals=0)
            self.logger.debug(f"{dev.name}: pvPower = {pv_power} (summed {len(PV_STRING_POWER_ADDRESSES)} PV strings)")
        except ModbusException as e:
            dev.setErrorStateOnServer("Modbus read error")
            self.logger.error(f"{dev.name}: {e}")
            return
        except Exception:
            # Anything not already modeled above (e.g. a decode bug) would
            # otherwise only get logged by runConcurrentThread's outer
            # try/except - leaving the device looking fine (green) in
            # Indigo's device list while it silently stops updating.
            dev.setErrorStateOnServer("Unexpected error - see plugin log")
            self.logger.exception(f"{dev.name}: unexpected error while polling")
            return
        finally:
            client.close()

        grid_power = values["gridPower"]
        battery_power = values["batteryPower"]
        # Matches the Hillview integration's "house load" formula: PV +
        # battery output + grid import all flow into the house, whichever
        # combination is currently supplying it.
        load_power = pv_power + battery_power + grid_power

        # decimalPlaces controls Indigo's own display formatting - plain
        # round() doesn't help here, since Indigo shows the raw double's full
        # binary expansion (e.g. "90.40000000000001") regardless of how
        # cleanly the Python float was rounded before being sent.
        dev.updateStatesOnServer([
            {"key": "pvPower", "value": int(pv_power)},
            {"key": "gridPower", "value": int(grid_power)},
            {"key": "batteryPower", "value": int(battery_power)},
            {"key": "batterySoC", "value": values["batterySoC"], "decimalPlaces": 2},
            {"key": "batterySoH", "value": values["batterySoH"], "decimalPlaces": 2},
            {"key": "loadPower", "value": int(load_power)},
            {"key": "lifetimeFeedToGrid", "value": values["lifetimeFeedToGrid"], "decimalPlaces": 2},
            {"key": "lifetimeConsumedFromGrid", "value": values["lifetimeConsumedFromGrid"], "decimalPlaces": 2},
        ])
        dev.setErrorStateOnServer(None)

    def dashboard(self, action, dev=None, caller_waiting_for_result=None):
        """Serve the live AlphaESS dashboard page.

        Reachable at ``http://<this-mac's-ip>:8176/message/<pluginId>/dashboard``
        via Indigo's built-in plugin HTTP responder. The page itself is static;
        it polls ``dashboard_data`` client-side for live values.

        Args:
            action (indigo.Dict): The inbound HTTP request wrapper Indigo provides.
            dev: Unused - required by Indigo's HTTP responder calling convention.
            caller_waiting_for_result: Unused - required by Indigo's HTTP responder calling convention.

        Returns:
            indigo.Dict: An HTTP reply dict (status/content/headers).
        """
        try:
            reply = indigo.Dict()
            reply["status"] = 200
            reply["content"] = DASHBOARD_HTML
            reply["headers"] = {"Content-Type": "text/html; charset=utf-8"}
            return reply
        except Exception:
            # Indigo's own exception-marshalling can itself fail ("unable to
            # convert python exception"), hiding the real cause - log it here
            # ourselves rather than relying on that bridge.
            self.logger.exception("Error serving dashboard")
            reply = indigo.Dict()
            reply["status"] = 500
            reply["content"] = "Internal error - see plugin log"
            reply["headers"] = {"Content-Type": "text/plain"}
            return reply

    def dashboard_data(self, action, dev=None, caller_waiting_for_result=None):
        """Serve the current inverter states as JSON, for the dashboard page to poll.

        Args:
            action (indigo.Dict): The inbound HTTP request wrapper Indigo provides;
                its ``props["url_query_args"]`` may contain a ``deviceId`` to pick
                a specific inverter device when more than one is configured.
            dev: Unused - required by Indigo's HTTP responder calling convention.
            caller_waiting_for_result: Unused - required by Indigo's HTTP responder calling convention.

        Returns:
            indigo.Dict: An HTTP reply dict wrapping a JSON body.
        """
        try:
            props = dict(action.props) if action is not None else {}
            query = props.get("url_query_args", {}) or {}
            requested_id = query.get("deviceId")

            devices = list(indigo.devices.iter("self.inverter"))
            device_list = [{"id": d.id, "name": d.name} for d in devices]

            target = None
            if requested_id:
                target = next((d for d in devices if str(d.id) == str(requested_id)), None)
            if target is None:
                target = next((d for d in devices if d.enabled and d.configured), None)

            reply = indigo.Dict()
            reply["status"] = 200
            reply["headers"] = {"Content-Type": "application/json"}

            if target is None:
                reply["content"] = json.dumps({"ok": False, "error": "No configured AlphaESS Inverter device found", "devices": device_list})
                return reply

            payload = {
                "ok": True,
                "deviceId": target.id,
                "deviceName": target.name,
                "devices": device_list,
                "errorState": target.errorState or None,
                "pvPower": target.states.get("pvPower", 0),
                "gridPower": target.states.get("gridPower", 0),
                "batteryPower": target.states.get("batteryPower", 0),
                "batterySoC": target.states.get("batterySoC", 0),
                "batterySoH": target.states.get("batterySoH", 0),
                "loadPower": target.states.get("loadPower", 0),
                "lifetimeFeedToGrid": target.states.get("lifetimeFeedToGrid", 0),
                "lifetimeConsumedFromGrid": target.states.get("lifetimeConsumedFromGrid", 0),
            }
            reply["content"] = json.dumps(payload)
            return reply
        except Exception:
            self.logger.exception("Error serving dashboard_data")
            reply = indigo.Dict()
            reply["status"] = 500
            reply["content"] = json.dumps({"ok": False, "error": "Internal error - see plugin log"})
            reply["headers"] = {"Content-Type": "application/json"}
            return reply
