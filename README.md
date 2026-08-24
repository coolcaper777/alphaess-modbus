# AlphaESS Modbus

An [Indigo Domotics](https://www.indigodomo.com/) plugin that reads **AlphaESS** hybrid inverter/battery telemetry over local Modbus TCP, exposing solar, grid, battery, and inverter data as native Indigo devices, plus a live browser dashboard.

- **Plugin ID:** `com.coolcaper.alphaessmodbus`
- **Requires:** Indigo 2022.1.2+ (Server API 3.1+)
- **Inverter connection:** local Modbus TCP over the inverter's LAN port — **not available over Wi-Fi**, no cloud/account required
- **Python dependency:** [`pymodbus`](https://pypi.org/project/pymodbus/) 3.15.0 (bundled)

> This is an unofficial, community-built plugin. It is not affiliated with or endorsed by AlphaESS. **Monitoring only** — it reads telemetry; it does not send dispatch/control commands (force charge/discharge, feed-in limits, etc.) to the inverter.

## Installation

1. Download the latest release's zip from the [Releases page](https://github.com/coolcaper777/alphaess-modbus/releases) — Safari extracts it automatically into a folder named `AlphaESS Modbus.indigoPlugin`.
   - *(Cloning the repo instead? Rename the checked-out folder to `AlphaESS Modbus.indigoPlugin` before installing, or Indigo won't recognize it as a plugin.)*
2. Double-click `AlphaESS Modbus.indigoPlugin` (or drag it onto the Indigo Server icon) to install, or copy it into `~/Library/Application Support/Perceptive Automation/Indigo <version>/Plugins/`.
3. Restart the plugin from Indigo's Plugins menu.
4. On first launch, Indigo installs the bundled Python dependency (`pymodbus`, from `requirements.txt`) automatically — no manual `pip install` needed.
5. Create an **AlphaESS Inverter** device with your inverter's local IP address. Its **AlphaESS Solar**, **AlphaESS Battery**, and **AlphaESS Grid** devices are then auto-discovered underneath it — no separate configuration needed for those three.

## What it does

- Polls the inverter's Modbus TCP interface once per cycle (default: every 30s, configurable per device) and fans the result out across four devices — AlphaESS inverters only accept **one** Modbus TCP connection at a time, so everything shares that single connection rather than each device polling independently.
- Computes house load as `pvPower + batteryPower + gridPower` — whichever combination is currently supplying the house.
- Serves a self-contained live dashboard (no login, polls every 5s) at:
  `http://<indigo-host>:8176/message/com.coolcaper.alphaessmodbus/dashboard`

## Devices

### AlphaESS Inverter (`inverter`)

The parent device — holds the connection config, auto-discovers the three device below, and carries whole-system values that don't belong to any one of them.

| Config field | Description |
|---|---|
| Inverter IP Address | Local IP of the inverter's LAN port |
| Modbus TCP Port | Default `502` |
| Modbus Unit/Slave ID | Default `85` (`0x55`) — AlphaESS inverters don't use the Modbus default of `1`; only change this if yours differs |
| Poll Interval (seconds) | Default `30` |

| State | Description |
|---|---|
| `loadPower` | Computed house load: `pvPower + batteryPower + gridPower` (W) |
| `invTemperature` | Inverter internal temperature (°C) |

### AlphaESS Solar (`solarDevice`)

Auto-created under the Inverter. `pvPower` plus a per-string voltage/current/power breakdown for all 6 MPPT inputs — useful for spotting one shaded/underperforming string. Unused strings simply read `0`, so partial installs (fewer than 6 strings wired up) need no configuration.

| State | Description |
|---|---|
| `pvPower` | Total solar power, summed across all 6 strings (W) |
| `pv1Voltage` … `pv6Voltage` | Per-string voltage (V) |
| `pv1Current` … `pv6Current` | Per-string current (A) |
| `pv1Power` … `pv6Power` | Per-string power (W) |

### AlphaESS Battery (`batteryDevice`)

Auto-created under the Inverter.

| State | Description |
|---|---|
| `batteryPower` | Battery power — positive = discharging, negative = charging (W) |
| `batterySoC` | State of charge (%) |
| `batterySoH` | State of health (%) |
| `batteryVoltage` / `batteryCurrent` | Pack voltage/current (V/A) |
| `batteryMinCellVoltage` / `batteryMaxCellVoltage` | Min/max individual cell voltage across the pack (V) — a widening gap between these is an early sign of cell imbalance |
| `batteryMinCellTemp` / `batteryMaxCellTemp` | Min/max individual cell temperature (°C) |
| `batteryCapacity` | Rated capacity (kWh) |
| `batteryChargeEnergy` / `batteryDischargeEnergy` | Lifetime energy charged/discharged (kWh) |

A battery-less installation is currently **untested** — if you run one and see a `Modbus read error` on this device rather than sensible `0` values, please open an issue with your plugin log. It won't affect the Solar or Grid devices either way, since each is read and reported independently.

### AlphaESS Grid (`gridDevice`)

Auto-created under the Inverter.

| State | Description |
|---|---|
| `gridPower` | Grid power — negative = exporting, positive = importing (W) |
| `gridVoltage` / `gridCurrent` / `gridFrequency` | Grid connection point readings (V/A/Hz) — useful for diagnosing grid quality issues |
| `lifetimeFeedToGrid` | Lifetime energy exported to grid (kWh) |
| `lifetimeConsumedFromGrid` | Lifetime energy imported from grid (kWh) |

## Debug logging

Plugin → Configure → **Enable Debug Logging** logs every Modbus register read/decode for each inverter in detail. It can be toggled live without restarting the plugin. Turn it on when a value looks wrong — the logged raw register values show exactly what the inverter returned before decoding.

## Troubleshooting

- **"Connection failed":** confirm the IP/port are correct and the inverter is reachable on your local network. AlphaESS inverters accept only **one** Modbus TCP connection at a time — if something else (e.g. Home Assistant) already holds that connection, this plugin's connection attempts will fail. A small TCP proxy (e.g. [`modbus-proxy`](https://github.com/tiagocoutinho/modbus-proxy)) in front of the inverter lets multiple clients share it.
- **"Modbus read error" on one of the three child devices:** a read failure isolated to that device's data only — Solar/Battery/Grid are each read and reported independently, so this doesn't affect the other two. Retries automatically on the next poll interval.
- **"Partial read this cycle - ... unavailable" on the Inverter device:** one or more of Solar/Battery/Grid failed to read this cycle (see that device for the specific error) — `loadPower` is skipped rather than computed from incomplete data until all three succeed again.
- **"Invalid Port or Unit ID":** the Port or Unit/Slave ID field isn't a plain number.
- **Dashboard shows "Could not reach the AlphaESS Modbus plugin":** the plugin isn't running, or the browser can't reach the Indigo host on port 8176.

## Upgrading from a version before the Solar/Battery/Grid split

If you have an existing **AlphaESS Inverter** device from before this device was split into four, `pvPower`/`gridPower`/`batteryPower`/`batterySoC`/`batterySoH`/`lifetimeFeedToGrid`/`lifetimeConsumedFromGrid` have moved off it onto the new Solar/Battery/Grid devices (auto-created the first time it polls successfully after upgrading). Any Control Pages or triggers referencing those states directly on the Inverter device will need to be re-pointed at the new devices.

## Credits

- **Plugin:** authored by [coolcaper777](https://github.com/coolcaper777).
- **Register map:** derived from [`SorX14/alphaess_modbus`](https://github.com/SorX14/alphaess_modbus) (MIT-licensed), cross-checked against the dispatch register documentation at [projects.hillviewlodge.ie/alphaess](https://projects.hillviewlodge.ie/alphaess/).
- **Modbus library:** [`pymodbus`](https://github.com/pymodbus-dev/pymodbus).
- **Hardware:** [AlphaESS](https://www.alphaess.com/) hybrid inverters.
- **This plugin and its documentation** were built with [Claude Code](https://claude.com/claude-code) (Anthropic).
