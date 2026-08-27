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

# runConcurrentThread ticks every 5 seconds (see below), so anything faster
# than that wouldn't actually poll any sooner - just enforced as a floor.
MIN_POLL_INTERVAL = 5
DEFAULT_POLL_INTERVAL = 30

# Inverter ConfigUI fields worth an Indigo Event Log confirmation line when
# edited on an existing device (see deviceUpdated) - keyed by pluginProps id,
# valued by the human-readable label to log it under.
INVERTER_CONFIG_FIELD_LABELS = {
    "address": "Inverter IP Address",
    "port": "Modbus TCP Port",
    "unitId": "Modbus Unit/Slave ID",
    "pollInterval": "Poll Interval",
}

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
    # Grid (cluster: 16-34). Phase B/C readings simply read 0 on a
    # single-phase installation - same "unused reads 0" pattern as the PV
    # strings, so no special-casing needed there either.
    "lifetimeFeedToGrid": {"address": 16, "words": 2, "signed": False, "decimals": 2},
    "lifetimeConsumedFromGrid": {"address": 18, "words": 2, "signed": False, "decimals": 2},
    # senalse/ha-alphaess-modbus's register_map.md documents these as
    # scale x1 by default (raw register value *is* the volt reading) - only
    # the SMILE-B3/SMILE-B3-PLUS model variant uses x0.1 instead. This
    # plugin previously used decimals:1 (x0.1) unconditionally; switched to
    # the documented default since invTemperature (which has the same
    # SMILE-B3 exception, just x0.01 instead of x0.1) has never shown a
    # wrong-looking value on this install, implying non-SMILE-B3 hardware.
    "gridVoltage": {"address": 20, "words": 1, "signed": False, "decimals": 0},
    "gridVoltageB": {"address": 21, "words": 1, "signed": False, "decimals": 0},
    "gridVoltageC": {"address": 22, "words": 1, "signed": False, "decimals": 0},
    # Grid current (23-25) deliberately NOT read - neither Hillview's
    # verified HA config nor senalse/ha-alphaess-modbus exposes it, unlike
    # every other register in this cluster, so there's no confirmed source
    # for its scale/reliability. User's call: stick to values we can be
    # certain about rather than guess. Current is derivable from
    # gridPower/gridVoltage anyway (I = P/V) if ever needed.
    "gridPowerA": {"address": 27, "words": 2, "signed": True, "decimals": 0},
    "gridPowerB": {"address": 29, "words": 2, "signed": True, "decimals": 0},
    "gridPowerC": {"address": 31, "words": 2, "signed": True, "decimals": 0},
    "gridPower": {"address": 33, "words": 2, "signed": True, "decimals": 0},

    # Battery (cluster: 256-295)
    "batteryVoltage": {"address": 256, "words": 1, "signed": False, "decimals": 1},
    "batteryCurrent": {"address": 257, "words": 1, "signed": True, "decimals": 1},
    "batterySoC": {"address": 258, "words": 1, "signed": False, "decimals": 1},
    # Undocumented in every AlphaESS Modbus project checked while this
    # register sat unused - EXCEPT senalse/ha-alphaess-modbus's own
    # "AlphaESS Battery Full" template, confirmed working against the
    # user's real hardware: battery_status == 1 means the battery is full.
    # No other codes are documented, so nothing else is inferred from it.
    "batteryStatus": {"address": 259, "words": 1, "signed": False, "decimals": 0},
    "batteryMinCellVoltage": {"address": 263, "words": 1, "signed": False, "decimals": 3},
    "batteryMaxCellVoltage": {"address": 266, "words": 1, "signed": False, "decimals": 3},
    "batteryMinCellTemp": {"address": 269, "words": 1, "signed": True, "decimals": 1},
    "batteryMaxCellTemp": {"address": 272, "words": 1, "signed": True, "decimals": 1},
    "batteryCapacity": {"address": 281, "words": 1, "signed": False, "decimals": 1},
    "batterySoH": {"address": 283, "words": 1, "signed": False, "decimals": 1},
    "batteryChargeEnergy": {"address": 288, "words": 2, "signed": False, "decimals": 1},
    "batteryDischargeEnergy": {"address": 290, "words": 2, "signed": False, "decimals": 1},
    "batteryPower": {"address": 294, "words": 1, "signed": True, "decimals": 0},
    "batteryRemainingTime": {"address": 295, "words": 1, "signed": False, "decimals": 0},

    # Solar / PV strings + inverter health (cluster: 1052-1088). Unused
    # strings simply read 0 - no special-casing needed for fewer than 6
    # strings wired up. registers.json lists pv3Power's type as a single
    # 16-bit "register", but the surrounding address spacing (pv4Voltage
    # starts 2 registers after pv3Power, same gap as every other string)
    # shows it's actually 2 words like the rest - treated as such here.
    #
    # gridFrequency lives here, NOT in the grid/meter cluster above, despite
    # the name - it's read from the inverter's own frequency sensor
    # (address 1052/0x041C), not the grid CT meter's register 26/0x001A
    # that registers.json documents under the "frequency_grid" name. Switched
    # after cross-checking against senalse/ha-alphaess-modbus's YAML, which
    # the user confirmed is verified working on their own hardware: that
    # project's "AlphaESS Inverter Grid Frequency" sensor reads address 1052
    # exclusively and never references register 26 at all. Register 26 was
    # also the source of the original 10x display bug (decimals:1 there
    # instead of :2) - reading a different, actually-documented register
    # instead of just patching that one's scale removes the guesswork.
    "gridFrequency": {"address": 1052, "words": 1, "signed": False, "decimals": 2},
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
    # 1078-1087 (inverter warning/fault flags, lifetime PV energy) fall in
    # this same documented, contiguous range but aren't decoded - nothing
    # currently needs them; reading past them costs nothing extra since the
    # cluster already has to span up to invWorkMode at 1088.
    "invWorkMode": {"address": 1088, "words": 1, "signed": False, "decimals": 0},
}

# inverter_work_mode's documented meaning beyond these two codes is
# "model-specific" (per both the Hillview register docs and the
# senalse/ha-alphaess-modbus project) - anything else is shown as a raw
# fallback rather than guessed.
INVERTER_WORK_MODE_LABELS = {1: "Normal", 2: "Bypass/EPS"}

PV_STRINGS = range(1, 7)
# Not 1053 - widened by 1 register to also cover gridFrequency at 1052 (see
# the REGISTERS comment above for why that value lives in this cluster).
SOLAR_CLUSTER_START = 1052
BATTERY_CLUSTER_START = 256
GRID_CLUSTER_START = 16
# Far from every other cluster (system_time_year_month/day_hour/minute_second),
# so it's its own small extra round trip rather than folded into an existing
# cluster's range.
SYSTEM_TIME_CLUSTER_START = 1856

# Every state each child device exposes, in display order - used by
# dashboard_data to hand the dashboard page every value it has (not just the
# handful summarized on the four top tiles), grouped per device the same way
# Devices.xml groups them.
INVERTER_STATE_KEYS = ["loadPower", "invTemperature", "invWorkMode", "systemTime"]
SOLAR_STATE_KEYS = ["pvPower"] + [f"pv{n}{suffix}" for n in PV_STRINGS for suffix in ("Voltage", "Current", "Power")]
BATTERY_STATE_KEYS = [
    "batteryPower", "batterySoC", "batterySoH", "batteryVoltage", "batteryCurrent",
    "batteryMinCellVoltage", "batteryMaxCellVoltage", "batteryMinCellTemp", "batteryMaxCellTemp",
    "batteryCapacity", "batteryChargeEnergy", "batteryDischargeEnergy", "batteryFull", "batteryRemainingTime",
]
GRID_STATE_KEYS = [
    "gridPower", "gridPowerA", "gridPowerB", "gridPowerC",
    "gridVoltage", "gridVoltageB", "gridVoltageC",
    "gridFrequency", "lifetimeFeedToGrid", "lifetimeConsumedFromGrid",
]

# Batched read ranges covering every address in REGISTERS, so a poll takes 4
# Modbus round trips instead of 20+. Each tuple is (start_address, count).
# Each cluster's success/failure is tracked independently in _poll_inverter -
# a failure on one (e.g. the battery cluster, on hardware with no battery)
# doesn't prevent the others from updating their own device.
REGISTER_CLUSTERS = [
    (GRID_CLUSTER_START, 19),      # 16-34: lifetime energy, per-phase grid readings, gridPower
    (SOLAR_CLUSTER_START, 37),     # 1052-1088: grid frequency, 6 PV strings, inverter temperature, work mode
    (BATTERY_CLUSTER_START, 40),   # 256-295: battery voltage/current/status/cells/energy/power/remaining time
    (SYSTEM_TIME_CLUSTER_START, 3),  # 1856-1858: inverter clock (year/month, day/hour, minute/second)
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
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; }
  .meta { color: var(--text-muted); font-size: 13px; }
  .headerMeta { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
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
  .details {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px; margin-top: 12px;
  }
  .detail-card {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  .detail-card h2 { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
  .rows { display: flex; flex-direction: column; }
  .row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 7px 0; border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  .row:last-child { border-bottom: none; }
  .row-label { color: var(--text-secondary); }
  .row-value { font-weight: 500; text-align: right; }
  .strings-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .strings-table th { text-align: right; color: var(--text-muted); font-weight: 500; padding: 4px 0 8px; }
  .strings-table th:first-child, .strings-table td:first-child { text-align: left; }
  .strings-table td { text-align: right; padding: 6px 0; border-top: 1px solid var(--border); }
  .empty-note { color: var(--text-muted); font-size: 13px; }
  footer { margin-top: 24px; color: var(--text-muted); font-size: 12px; }
</style>
</head>
<body>
<div class="page">
  <header>
    <div>
      <h1 id="deviceName">AlphaESS Inverter</h1>
      <div class="headerMeta" id="inverterMeta"></div>
    </div>
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

  <div class="details">
    <div class="detail-card">
      <h2><span class="dot dot-solar" style="display:inline-block;"></span> Solar strings</h2>
      <div id="solarDetail"><span class="empty-note">No solar device yet</span></div>
    </div>
    <div class="detail-card">
      <h2><span class="dot dot-battery" style="display:inline-block;"></span> Battery detail</h2>
      <div class="rows" id="batteryDetail"><span class="empty-note">No battery device yet</span></div>
    </div>
    <div class="detail-card">
      <h2><span class="dot dot-grid" style="display:inline-block;"></span> Grid detail</h2>
      <div class="rows" id="gridDetail"><span class="empty-note">No grid device yet</span></div>
    </div>
  </div>

  <footer>Auto-refreshes every 5 seconds.</footer>
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

  function fmt(value, decimals, unit) {
    return Number(value).toFixed(decimals) + " " + unit;
  }

  function formatMinutes(minutes) {
    minutes = Math.round(minutes);
    if (minutes < 60) return minutes + " min";
    var hours = Math.floor(minutes / 60);
    var rem = minutes % 60;
    return hours + "h " + rem + "m";
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

  // Builds one label/value line (used by the Battery/Grid detail cards) purely
  // via createElement/textContent - never innerHTML - so a device renamed to
  // contain HTML/script content can't inject into the page.
  function row(label, valueText) {
    var el = document.createElement("div");
    el.className = "row";
    var labelEl = document.createElement("span");
    labelEl.className = "row-label";
    labelEl.textContent = label;
    var valueEl = document.createElement("span");
    valueEl.className = "row-value";
    valueEl.textContent = valueText;
    el.appendChild(labelEl);
    el.appendChild(valueEl);
    return el;
  }

  function renderBatteryDetail(battery) {
    var container = document.getElementById("batteryDetail");
    container.replaceChildren();
    if (!battery) {
      container.appendChild(Object.assign(document.createElement("span"), {className: "empty-note", textContent: "No battery device yet"}));
      return;
    }
    container.appendChild(row("Voltage", fmt(battery.batteryVoltage, 1, "V")));
    container.appendChild(row("Current", fmt(battery.batteryCurrent, 1, "A")));
    container.appendChild(row("State of health", fmt(battery.batterySoH, 1, "%")));
    container.appendChild(row("Min / max cell temp", fmt(battery.batteryMinCellTemp, 1, "\u00b0C") + " / " + fmt(battery.batteryMaxCellTemp, 1, "\u00b0C")));
    container.appendChild(row("Rated capacity", fmt(battery.batteryCapacity, 1, "kWh")));
    container.appendChild(row("Lifetime charge", fmt(battery.batteryChargeEnergy, 1, "kWh")));
    container.appendChild(row("Lifetime discharge", fmt(battery.batteryDischargeEnergy, 1, "kWh")));
    if (battery.batteryFull) {
      container.appendChild(row("Status", "Full"));
    } else if (battery.batteryRemainingTime > 0) {
      var label = battery.batteryPower > 0 ? "Time to empty" : "Time to full";
      container.appendChild(row(label, formatMinutes(battery.batteryRemainingTime)));
    }
  }

  // A phase reads a flat 0 V when it isn't wired up (same "unused reads 0"
  // convention as the PV strings) - a small noise floor avoids treating
  // sensor jitter on an unused phase as a real 3-phase installation.
  function isThreePhase(grid) {
    return Math.abs(grid.gridVoltageB) > 1 || Math.abs(grid.gridVoltageC) > 1;
  }

  function renderGridDetail(grid) {
    var container = document.getElementById("gridDetail");
    container.replaceChildren();
    if (!grid) {
      container.appendChild(Object.assign(document.createElement("span"), {className: "empty-note", textContent: "No grid device yet"}));
      return;
    }
    if (isThreePhase(grid)) {
      var table = document.createElement("table");
      table.className = "strings-table";
      var thead = document.createElement("tr");
      ["Phase", "Voltage", "Power"].forEach(function (h) {
        var th = document.createElement("th");
        th.textContent = h;
        thead.appendChild(th);
      });
      table.appendChild(thead);
      [
        ["A", grid.gridVoltage, grid.gridPowerA],
        ["B", grid.gridVoltageB, grid.gridPowerB],
        ["C", grid.gridVoltageC, grid.gridPowerC],
      ].forEach(function (phase) {
        var tr = document.createElement("tr");
        [phase[0], fmt(phase[1], 0, "V"), formatPower(phase[2])].forEach(function (text) {
          var td = document.createElement("td");
          td.textContent = text;
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      container.appendChild(table);
    } else {
      container.appendChild(row("Voltage", fmt(grid.gridVoltage, 0, "V")));
    }
    container.appendChild(row("Frequency", fmt(grid.gridFrequency, 2, "Hz")));
    container.appendChild(row("Lifetime fed to grid", fmt(grid.lifetimeFeedToGrid, 2, "kWh")));
    container.appendChild(row("Lifetime consumed from grid", fmt(grid.lifetimeConsumedFromGrid, 2, "kWh")));
  }

  function renderSolarDetail(solar) {
    var container = document.getElementById("solarDetail");
    container.replaceChildren();
    if (!solar) {
      container.appendChild(Object.assign(document.createElement("span"), {className: "empty-note", textContent: "No solar device yet"}));
      return;
    }
    var table = document.createElement("table");
    table.className = "strings-table";
    var thead = document.createElement("tr");
    ["String", "Voltage", "Current", "Power"].forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      thead.appendChild(th);
    });
    table.appendChild(thead);
    for (var n = 1; n <= 6; n++) {
      var tr = document.createElement("tr");
      var cells = [
        "PV" + n,
        fmt(solar["pv" + n + "Voltage"], 1, "V"),
        fmt(solar["pv" + n + "Current"], 1, "A"),
        formatPower(solar["pv" + n + "Power"])
      ];
      cells.forEach(function (text) {
        var td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      });
      table.appendChild(tr);
    }
    container.appendChild(table);
  }

  function render(data) {
    document.getElementById("deviceName").textContent = data.deviceName || "AlphaESS Inverter";
    document.getElementById("lastUpdated").textContent = "Updated " + new Date().toLocaleTimeString();

    var metaParts = [];
    if (data.inverter) {
      if (typeof data.inverter.invTemperature === "number") metaParts.push("Inverter " + fmt(data.inverter.invTemperature, 1, "\u00b0C"));
      if (data.inverter.invWorkMode) metaParts.push(data.inverter.invWorkMode);
      if (data.inverter.systemTime) metaParts.push(data.inverter.systemTime);
    }
    document.getElementById("inverterMeta").textContent = metaParts.join(" \u00b7 ");

    var solar = data.solar, battery = data.battery, grid = data.grid;

    document.getElementById("pvPower").textContent = formatPower(solar ? solar.pvPower : 0);
    document.getElementById("gridPower").textContent = formatPower(Math.abs(grid ? grid.gridPower : 0));
    document.getElementById("gridDirection").textContent = !grid ? "-" :
      (grid.gridPower < 0 ? "\u2190 Exporting to grid" : (grid.gridPower > 0 ? "\u2192 Importing from grid" : "Idle"));

    document.getElementById("batterySoC").textContent = battery ? fmt(battery.batterySoC, 1, "%") : "-";
    var bp = battery ? battery.batteryPower : 0;
    document.getElementById("batteryDirection").textContent = !battery ? "-" :
      battery.batteryFull ? "\u2713 Full"
      : (Math.abs(bp) < 15 ? (formatPower(bp) + " \u00b7 Idle")
        : (bp > 0 ? "\u2193 Discharging \u00b7 " + formatPower(bp) : "\u2191 Charging \u00b7 " + formatPower(Math.abs(bp))));

    document.getElementById("loadPower").textContent = formatPower(data.inverter ? data.inverter.loadPower : 0);

    renderSolarDetail(solar);
    renderBatteryDetail(battery);
    renderGridDetail(grid);

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


def _decode_system_time(registers: list) -> str:
    """Combine the inverter clock's 3 packed registers into a display string.

    Each register packs two plain (non-BCD) byte values - confirmed against
    SorX14/alphaess_modbus's own formatter.py, which decodes the same
    registers the same way.

    Args:
        registers (list): The 3 raw registers read from SYSTEM_TIME_CLUSTER_START,
            in order: year/month, day/hour, minute/second.

    Returns:
        str: ``"YYYY-MM-DD HH:MM:SS"``.
    """
    year_month, day_hour, minute_second = registers
    year, month = 2000 + (year_month >> 8), year_month & 0xFF
    day, hour = day_hour >> 8, day_hour & 0xFF
    minute, second = minute_second >> 8, minute_second & 0xFF
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


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
            poll_interval = valuesDict.get("pollInterval", "").strip()
            try:
                if int(poll_interval) < MIN_POLL_INTERVAL:
                    errors_dict["pollInterval"] = f"Poll interval must be at least {MIN_POLL_INTERVAL} seconds."
            except ValueError:
                errors_dict["pollInterval"] = "Poll interval must be a whole number of seconds."
        elif typeId in ("solarDevice", "batteryDevice", "gridDevice"):
            if not valuesDict.get("systemDevice", ""):
                errors_dict["systemDevice"] = "Please select the AlphaESS Inverter this device belongs to."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    def deviceUpdated(self, origDev: indigo.Device, newDev: indigo.Device) -> None:
        """Indigo calls this whenever any device's properties or states change.

        Used here purely to log a confirmation line when a config field (e.g. Poll
        Interval) is actually saved with a new value - fires on every poll's state
        update too, but comparing ``pluginProps`` (unaffected by state updates) rather
        than the whole device keeps that from producing log spam every poll cycle.

        Args:
            origDev (indigo.Device): The device's state before the change.
            newDev (indigo.Device): The device's state after the change.
        """
        super().deviceUpdated(origDev, newDev)
        if newDev.pluginId != self.pluginId or newDev.deviceTypeId != "inverter":
            return
        old_props = origDev.pluginProps
        new_props = newDev.pluginProps
        for field_id, label in INVERTER_CONFIG_FIELD_LABELS.items():
            # Only present in old_props for an already-existing device being
            # edited - absent on first save of a brand new device, which isn't
            # a "change" worth logging.
            if field_id not in old_props:
                continue
            old_value, new_value = old_props.get(field_id), new_props.get(field_id)
            if old_value == new_value:
                continue
            unit = "s" if field_id == "pollInterval" else ""
            self.logger.info(f"{newDev.name}: {label} changed from {old_value}{unit} to {new_value}{unit}")

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
                    try:
                        interval = max(MIN_POLL_INTERVAL, int(dev.pluginProps.get("pollInterval", DEFAULT_POLL_INTERVAL)))
                    except (TypeError, ValueError):
                        # validateDeviceConfigUi rejects bad values going forward, but a
                        # device saved before that check existed could still have one on
                        # disk - falling back here keeps that one device's bad value from
                        # taking down polling for every other configured inverter.
                        interval = DEFAULT_POLL_INTERVAL
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
        time_ok = SYSTEM_TIME_CLUSTER_START in cluster_registers

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
                {"key": "gridPowerA", "value": int(values["gridPowerA"])},
                {"key": "gridPowerB", "value": int(values["gridPowerB"])},
                {"key": "gridPowerC", "value": int(values["gridPowerC"])},
                {"key": "gridVoltage", "value": int(values["gridVoltage"])},
                {"key": "gridVoltageB", "value": int(values["gridVoltageB"])},
                {"key": "gridVoltageC", "value": int(values["gridVoltageC"])},
                {"key": "lifetimeFeedToGrid", "value": values["lifetimeFeedToGrid"], "decimalPlaces": 2},
                {"key": "lifetimeConsumedFromGrid", "value": values["lifetimeConsumedFromGrid"], "decimalPlaces": 2},
            ])
            grid_dev.setErrorStateOnServer(None)
        else:
            grid_dev.setErrorStateOnServer(cluster_errors.get(GRID_CLUSTER_START, "Modbus read error"))

        if solar_ok:
            # gridFrequency is read via the solar/inverter-health cluster
            # (see the REGISTERS comment for why) - updated on the Grid
            # device independently of grid_ok/gridCluster's own success.
            grid_dev.updateStatesOnServer([{"key": "gridFrequency", "value": values["gridFrequency"], "decimalPlaces": 2}])

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
                {"key": "batteryFull", "value": int(values["batteryStatus"]) == 1},
                {"key": "batteryRemainingTime", "value": int(values["batteryRemainingTime"])},
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
            inverter_states.append({
                "key": "invWorkMode",
                "value": INVERTER_WORK_MODE_LABELS.get(int(values["invWorkMode"]), f"Unknown work mode ({int(values['invWorkMode'])})"),
            })
        if time_ok:
            inverter_states.append({"key": "systemTime", "value": _decode_system_time(cluster_registers[SYSTEM_TIME_CLUSTER_START])})
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
            # child devices, not on the parent inverter device itself - each
            # gets handed to the dashboard page as its own object (null if that
            # child hasn't been created yet), carrying every state it has
            # rather than just the handful the top summary tiles need.
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
                "inverter": {k: target.states.get(k, 0) for k in INVERTER_STATE_KEYS},
                "solar": {k: solar.states.get(k, 0) for k in SOLAR_STATE_KEYS} if solar else None,
                "battery": {k: battery.states.get(k, 0) for k in BATTERY_STATE_KEYS} if battery else None,
                "grid": {k: grid.states.get(k, 0) for k in GRID_STATE_KEYS} if grid else None,
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
