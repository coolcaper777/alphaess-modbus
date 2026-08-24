try:
    import indigo
except ImportError:
    pass

import json
import logging
import re
import time
from typing import Optional

# pymodbus is declared in requirements.txt and installed automatically by
# Indigo on first launch (same mechanism Automate Pulse 2 uses for aiopulse2) -
# no manual sys.path/vendoring needed.
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# Indigo's placeholder name for a device that hasn't been renamed yet
# (e.g. "new device", "new device 2" if there's a name collision) - child
# device creation is deferred while this still matches, same as MyAir, to
# avoid permanently naming children after the placeholder.
NEW_DEVICE_NAME_RE = re.compile(r"^new device(\s+\d+)?$", re.IGNORECASE)

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
# Grouped by which of the three child devices (Grid/Battery/Solar) each value
# is written to - see _poll_inverter. Every entry here falls inside one of the
# three REGISTER_CLUSTERS ranges below; nothing here costs an extra Modbus
# round trip over what the plugin already reads.
REGISTERS = {
    # Grid (cluster: 16-34)
    "lifetimeFeedToGrid": {"address": 16, "words": 2, "signed": False, "decimals": 2},
    "lifetimeConsumedFromGrid": {"address": 18, "words": 2, "signed": False, "decimals": 2},
    "gridVoltage": {"address": 20, "words": 1, "signed": False, "decimals": 1},
    "gridCurrent": {"address": 23, "words": 1, "signed": True, "decimals": 1},
    "gridFrequency": {"address": 26, "words": 1, "signed": False, "decimals": 1},
    "gridPower": {"address": 33, "words": 2, "signed": True, "decimals": 0},

    # Battery (cluster: 256-294)
    "batteryVoltage": {"address": 256, "words": 1, "signed": False, "decimals": 1},
    "batteryCurrent": {"address": 257, "words": 1, "signed": True, "decimals": 1},
    "batterySoC": {"address": 258, "words": 1, "signed": False, "decimals": 1},
    "batteryMinCellVoltage": {"address": 263, "words": 1, "signed": False, "decimals": 3},
    "batteryMaxCellVoltage": {"address": 266, "words": 1, "signed": False, "decimals": 3},
    "batteryMinCellTemp": {"address": 269, "words": 1, "signed": True, "decimals": 1},
    "batteryMaxCellTemp": {"address": 272, "words": 1, "signed": True, "decimals": 1},
    "batteryCapacity": {"address": 281, "words": 1, "signed": False, "decimals": 1},
    "batterySoH": {"address": 283, "words": 1, "signed": False, "decimals": 1},
    "batteryChargeEnergy": {"address": 288, "words": 2, "signed": False, "decimals": 1},
    "batteryDischargeEnergy": {"address": 290, "words": 2, "signed": False, "decimals": 1},
    "batteryPower": {"address": 294, "words": 1, "signed": True, "decimals": 0},

    # Solar / PV strings + inverter health (cluster: 1053-1077). Unused
    # strings simply read 0 - no special-casing needed for fewer than 6
    # strings wired up. registers.json lists pv3Power's type as a single
    # 16-bit "register", but the surrounding address spacing (pv4Voltage
    # starts 2 registers after pv3Power, same gap as every other string)
    # shows it's actually 2 words like the rest - treated as such here.
    "pv1Voltage": {"address": 1053, "words": 1, "signed": False, "decimals": 1},
    "pv1Current": {"address": 1054, "words": 1, "signed": False, "decimals": 1},
    "pv1Power": {"address": 1055, "words": 2, "signed": False, "decimals": 0},
    "pv2Voltage": {"address": 1057, "words": 1, "signed": False, "decimals": 1},
    "pv2Current": {"address": 1058, "words": 1, "signed": False, "decimals": 1},
    "pv2Power": {"address": 1059, "words": 2, "signed": False, "decimals": 0},
    "pv3Voltage": {"address": 1061, "words": 1, "signed": False, "decimals": 1},
    "pv3Current": {"address": 1062, "words": 1, "signed": False, "decimals": 1},
    "pv3Power": {"address": 1063, "words": 2, "signed": False, "decimals": 0},
    "pv4Voltage": {"address": 1065, "words": 1, "signed": False, "decimals": 1},
    "pv4Current": {"address": 1066, "words": 1, "signed": False, "decimals": 1},
    "pv4Power": {"address": 1067, "words": 2, "signed": False, "decimals": 0},
    "pv5Voltage": {"address": 1069, "words": 1, "signed": False, "decimals": 1},
    "pv5Current": {"address": 1070, "words": 1, "signed": False, "decimals": 1},
    "pv5Power": {"address": 1071, "words": 2, "signed": False, "decimals": 0},
    "pv6Voltage": {"address": 1073, "words": 1, "signed": False, "decimals": 1},
    "pv6Current": {"address": 1074, "words": 1, "signed": False, "decimals": 1},
    "pv6Power": {"address": 1075, "words": 2, "signed": False, "decimals": 0},
    "invTemperature": {"address": 1077, "words": 1, "signed": False, "decimals": 1},
}

PV_STRINGS = range(1, 7)
SOLAR_CLUSTER_START = 1053
BATTERY_CLUSTER_START = 256
GRID_CLUSTER_START = 16

# Batched read ranges covering every address in REGISTERS, so a poll takes 3
# Modbus round trips instead of 20+. Each tuple is (start_address, count).
# Each cluster's success/failure is tracked independently in _poll_inverter -
# a failure on one (e.g. the battery cluster, on hardware with no battery)
# doesn't prevent the other two from updating their own device.
REGISTER_CLUSTERS = [
    (GRID_CLUSTER_START, 19),    # 16-34: lifetime energy, per-phase grid readings, gridPower
    (SOLAR_CLUSTER_START, 25),   # 1053-1077: 6 PV strings + inverter temperature
    (BATTERY_CLUSTER_START, 39),  # 256-294: battery voltage/current/cells/energy/power
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

    def validateDeviceConfigUi(self, valuesDict: indigo.Dict, typeId: str, devId: int) -> tuple:
        """Validate the New/Edit Device dialog before it's allowed to save.

        Args:
            valuesDict (indigo.Dict): The dialog's current field values.
            typeId (str): The device type being configured.
            devId (int): The device's ID (0 for a device being newly created).

        Returns:
            tuple: ``(True, valuesDict)`` if valid, or
                ``(False, valuesDict, errorsDict)`` with per-field error messages
                if not.
        """
        errors_dict = indigo.Dict()
        if typeId == "inverter":
            address = valuesDict.get("address", "").strip()
            if not address:
                errors_dict["address"] = "Inverter IP address is required."
            elif " " in address:
                errors_dict["address"] = "IP address must not contain spaces."
        elif typeId in ("solarDevice", "batteryDevice", "gridDevice"):
            if not valuesDict.get("systemDevice", ""):
                errors_dict["systemDevice"] = "Please select the AlphaESS Inverter this device belongs to."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    def get_inverters(self, filter: str = "", valuesDict: Optional[indigo.Dict] = None, typeId: str = "", targetId: int = 0) -> list:
        """Dynamic menu list for the Solar/Battery/Grid device's 'AlphaESS Inverter' picker.

        Args:
            filter (str): Indigo's dynamic-list filter string (unused).
            valuesDict (Optional[indigo.Dict]): The dialog's current field values (unused).
            typeId (str): The device type being configured (unused).
            targetId (int): The device's ID (unused).

        Returns:
            list: ``(device_id, device_name)`` tuples for every AlphaESS Inverter device.
        """
        return [(dev.id, dev.name) for dev in indigo.devices.iter("self.inverter")]

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

    def _get_or_create_child(self, parent_dev: indigo.Device, type_id: str, label: str) -> indigo.Device:
        """Return a parent inverter's Solar/Battery/Grid child device, creating it if missing.

        Args:
            parent_dev (indigo.Device): The AlphaESS Inverter device.
            type_id (str): The child device type ID (``solarDevice``/``batteryDevice``/``gridDevice``).
            label (str): Human-readable label used for the auto-generated name and log line.

        Returns:
            indigo.Device: The existing or newly-created child device.
        """
        for d in indigo.devices.iter(f"self.{type_id}"):
            if d.pluginProps.get("systemDevice") == str(parent_dev.id):
                return d
        new_dev = indigo.device.create(
            protocol=indigo.kProtocol.Plugin,
            deviceTypeId=type_id,
            name=f"{parent_dev.name} - {label}",
            pluginId=self.pluginId,
            props={"systemDevice": str(parent_dev.id)},
        )
        self.logger.info(f"Created {label.lower()} device: {new_dev.name}")
        return new_dev

    def _find_child(self, parent_id: int, type_id: str) -> Optional[indigo.Device]:
        """Look up (without creating) a parent inverter's Solar/Battery/Grid child device.

        Args:
            parent_id (int): The parent AlphaESS Inverter device's ID.
            type_id (str): The child device type ID (``solarDevice``/``batteryDevice``/``gridDevice``).

        Returns:
            Optional[indigo.Device]: The child device, or None if it doesn't exist (yet).
        """
        return next(
            (d for d in indigo.devices.iter(f"self.{type_id}") if d.pluginProps.get("systemDevice") == str(parent_id)),
            None,
        )

    def _set_children_error(self, dev: indigo.Device, message: str) -> None:
        """Set an Indigo error state on every already-existing child device of an inverter.

        Used for failures that happen before any register cluster is even
        attempted (bad config, connect failure) - existing children shouldn't
        be left showing stale "last good" data with no error indicator.
        Deliberately doesn't create children that don't exist yet.

        Args:
            dev (indigo.Device): The parent AlphaESS Inverter device.
            message (str): The error message to set on each child.
        """
        for type_id in ("solarDevice", "batteryDevice", "gridDevice"):
            child = self._find_child(dev.id, type_id)
            if child:
                child.setErrorStateOnServer(message)

    def _poll_inverter(self, dev: indigo.Device) -> None:
        """Read one poll's worth of Modbus registers from an inverter and update its (and its
        Solar/Battery/Grid children's) states.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device to poll.
        """
        address = dev.pluginProps.get("address", "")
        if not address:
            dev.setErrorStateOnServer("No IP address configured")
            self._set_children_error(dev, "No IP address configured")
            return
        try:
            port = int(dev.pluginProps.get("port", 502))
            unit_id = int(dev.pluginProps.get("unitId", DEFAULT_UNIT_ID))
        except (TypeError, ValueError):
            # Both fields are plain textfields in Devices.xml - Indigo doesn't
            # enforce numeric-only input, so a cleared/typo'd field would
            # otherwise raise here unguarded, before the try block below that
            # actually sets an error state on failure.
            message = "Invalid Port or Unit ID - must be a number"
            dev.setErrorStateOnServer(message)
            self._set_children_error(dev, message)
            self.logger.error(
                f"{dev.name}: Port/Unit ID must be numeric "
                f"(got port={dev.pluginProps.get('port')!r}, unitId={dev.pluginProps.get('unitId')!r})"
            )
            return

        client = ModbusTcpClient(address, port=port, timeout=5)
        if not client.connect():
            message = "Connection failed"
            dev.setErrorStateOnServer(message)
            self._set_children_error(dev, message)
            self.logger.error(f"{dev.name}: could not connect to {address}:{port}")
            return

        try:
            values = {}
            cluster_registers = {}
            cluster_errors = {}
            for start, count in REGISTER_CLUSTERS:
                try:
                    result = client.read_holding_registers(start, count=count, device_id=unit_id)
                    if result.isError():
                        raise ModbusException(f"error reading registers {start}-{start + count - 1}: {result}")
                except ModbusException as e:
                    # Isolated per cluster rather than aborting the whole poll -
                    # e.g. a battery-less installation refusing the battery
                    # cluster shouldn't also stop Grid/Solar from updating.
                    cluster_errors[start] = str(e)
                    self.logger.error(f"{dev.name}: {e}")
                    continue
                cluster_registers[start] = result.registers
                for name, spec in REGISTERS.items():
                    if start <= spec["address"] and spec["address"] + spec["words"] <= start + count:
                        offset = spec["address"] - start
                        regs = result.registers[offset: offset + spec["words"]]
                        values[name] = _decode_value(regs, spec["signed"], spec["decimals"])
                        self.logger.debug(f"{dev.name}: {name} = {values[name]} (raw {regs})")
        except Exception:
            # Anything not already modeled above (e.g. a decode bug) would
            # otherwise only get logged by runConcurrentThread's outer
            # try/except - leaving the device looking fine (green) in
            # Indigo's device list while it silently stops updating.
            message = "Unexpected error - see plugin log"
            dev.setErrorStateOnServer(message)
            self._set_children_error(dev, message)
            self.logger.exception(f"{dev.name}: unexpected error while polling")
            return
        finally:
            client.close()

        grid_ok = GRID_CLUSTER_START in cluster_registers
        battery_ok = BATTERY_CLUSTER_START in cluster_registers
        solar_ok = SOLAR_CLUSTER_START in cluster_registers

        if NEW_DEVICE_NAME_RE.match(dev.name.strip()):
            # Skip creating/updating child devices while this still has
            # Indigo's placeholder name - they'd get named after it
            # permanently. The next poll (after it's renamed) picks this back up.
            self.logger.debug(f"Deferring child device creation for \"{dev.name}\" until it's renamed")
            return

        solar_dev = self._get_or_create_child(dev, "solarDevice", "Solar")
        battery_dev = self._get_or_create_child(dev, "batteryDevice", "Battery")
        grid_dev = self._get_or_create_child(dev, "gridDevice", "Grid")

        # decimalPlaces controls Indigo's own display formatting - plain
        # round() doesn't help here, since Indigo shows the raw double's full
        # binary expansion (e.g. "90.40000000000001") regardless of how
        # cleanly the Python float was rounded before being sent.
        if grid_ok:
            grid_dev.updateStatesOnServer([
                {"key": "gridPower", "value": int(values["gridPower"])},
                {"key": "gridVoltage", "value": values["gridVoltage"], "decimalPlaces": 1},
                {"key": "gridCurrent", "value": values["gridCurrent"], "decimalPlaces": 1},
                {"key": "gridFrequency", "value": values["gridFrequency"], "decimalPlaces": 1},
                {"key": "lifetimeFeedToGrid", "value": values["lifetimeFeedToGrid"], "decimalPlaces": 2},
                {"key": "lifetimeConsumedFromGrid", "value": values["lifetimeConsumedFromGrid"], "decimalPlaces": 2},
            ])
            grid_dev.setErrorStateOnServer(None)
        else:
            grid_dev.setErrorStateOnServer(cluster_errors.get(GRID_CLUSTER_START, "Modbus read error"))

        if battery_ok:
            battery_dev.updateStatesOnServer([
                {"key": "batteryPower", "value": int(values["batteryPower"])},
                {"key": "batterySoC", "value": values["batterySoC"], "decimalPlaces": 2},
                {"key": "batterySoH", "value": values["batterySoH"], "decimalPlaces": 2},
                {"key": "batteryVoltage", "value": values["batteryVoltage"], "decimalPlaces": 1},
                {"key": "batteryCurrent", "value": values["batteryCurrent"], "decimalPlaces": 1},
                {"key": "batteryMinCellVoltage", "value": values["batteryMinCellVoltage"], "decimalPlaces": 3},
                {"key": "batteryMaxCellVoltage", "value": values["batteryMaxCellVoltage"], "decimalPlaces": 3},
                {"key": "batteryMinCellTemp", "value": values["batteryMinCellTemp"], "decimalPlaces": 1},
                {"key": "batteryMaxCellTemp", "value": values["batteryMaxCellTemp"], "decimalPlaces": 1},
                {"key": "batteryCapacity", "value": values["batteryCapacity"], "decimalPlaces": 1},
                {"key": "batteryChargeEnergy", "value": values["batteryChargeEnergy"], "decimalPlaces": 1},
                {"key": "batteryDischargeEnergy", "value": values["batteryDischargeEnergy"], "decimalPlaces": 1},
            ])
            battery_dev.setErrorStateOnServer(None)
        else:
            battery_dev.setErrorStateOnServer(cluster_errors.get(BATTERY_CLUSTER_START, "Modbus read error"))

        pv_power = None
        if solar_ok:
            pv_power = sum(values[f"pv{n}Power"] for n in PV_STRINGS)
            solar_states = [{"key": "pvPower", "value": int(pv_power)}]
            for n in PV_STRINGS:
                solar_states.append({"key": f"pv{n}Voltage", "value": values[f"pv{n}Voltage"], "decimalPlaces": 1})
                solar_states.append({"key": f"pv{n}Current", "value": values[f"pv{n}Current"], "decimalPlaces": 1})
                solar_states.append({"key": f"pv{n}Power", "value": int(values[f"pv{n}Power"])})
            solar_dev.updateStatesOnServer(solar_states)
            solar_dev.setErrorStateOnServer(None)
            self.logger.debug(f"{dev.name}: pvPower = {pv_power} (summed 6 PV strings)")
        else:
            solar_dev.setErrorStateOnServer(cluster_errors.get(SOLAR_CLUSTER_START, "Modbus read error"))

        inverter_states = []
        if solar_ok:
            inverter_states.append({"key": "invTemperature", "value": values["invTemperature"], "decimalPlaces": 1})
        if grid_ok and battery_ok and solar_ok:
            # Matches the Hillview integration's "house load" formula: PV +
            # battery output + grid import all flow into the house, whichever
            # combination is currently supplying it. Skipped entirely (rather
            # than computed from a stale/partial mix) if any one of the three
            # clusters failed this cycle.
            load_power = pv_power + values["batteryPower"] + values["gridPower"]
            inverter_states.append({"key": "loadPower", "value": int(load_power)})
        if inverter_states:
            dev.updateStatesOnServer(inverter_states)

        if grid_ok and battery_ok and solar_ok:
            dev.setErrorStateOnServer(None)
        else:
            missing = [name for name, ok in (("solar", solar_ok), ("battery", battery_ok), ("grid", grid_ok)) if not ok]
            dev.setErrorStateOnServer(f"Partial read this cycle - {'/'.join(missing)} unavailable (see that device)")

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

            # pvPower/gridPower/batteryPower/etc. live on the Solar/Battery/Grid
            # child devices, not on the parent inverter device itself - the
            # dashboard's own JS is unchanged, it just gets its numbers
            # gathered from four devices instead of one now.
            solar = self._find_child(target.id, "solarDevice")
            battery = self._find_child(target.id, "batteryDevice")
            grid = self._find_child(target.id, "gridDevice")
            error_state = target.errorState or (solar and solar.errorState) or (battery and battery.errorState) or (grid and grid.errorState) or None

            payload = {
                "ok": True,
                "deviceId": target.id,
                "deviceName": target.name,
                "devices": device_list,
                "errorState": error_state,
                "pvPower": solar.states.get("pvPower", 0) if solar else 0,
                "gridPower": grid.states.get("gridPower", 0) if grid else 0,
                "batteryPower": battery.states.get("batteryPower", 0) if battery else 0,
                "batterySoC": battery.states.get("batterySoC", 0) if battery else 0,
                "batterySoH": battery.states.get("batterySoH", 0) if battery else 0,
                "loadPower": target.states.get("loadPower", 0),
                "lifetimeFeedToGrid": grid.states.get("lifetimeFeedToGrid", 0) if grid else 0,
                "lifetimeConsumedFromGrid": grid.states.get("lifetimeConsumedFromGrid", 0) if grid else 0,
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
