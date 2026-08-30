# AlphaESS Modbus

An [Indigo Domotics](https://www.indigodomo.com/) plugin that reads **AlphaESS** hybrid inverter/battery telemetry over local Modbus TCP, exposing solar, grid, battery, and inverter data as native Indigo devices, plus a live browser dashboard.

- **Plugin ID:** `com.coolcaper.alphaessmodbus`
- **Requires:** Indigo 2022.1.2+ (Server API 3.1+)
- **Inverter connection:** local Modbus TCP over the inverter's LAN port — **not available over Wi-Fi**, no cloud/account required
- **Python dependency:** [`pymodbus`](https://pypi.org/project/pymodbus/) 3.15.0 (bundled)

> This is an unofficial, community-built plugin. It is not affiliated with or endorsed by AlphaESS. It reads telemetry and also exposes **Force Charging**/**Force Discharging**/**Dispatch**/**Dispatch Reset** actions (see below) built on AlphaESS's Dispatch mechanism, which is not stored in the inverter's flash memory — safe to use as often as needed. The inverter's own internal scheduler settings (max feed-to-grid, charge/discharge cutoff SoC and time periods) *are* flash-backed and are deliberately never written by this plugin.

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
| Poll Interval | Dropdown: 5s (not recommended)/10s/15s/30s (default)/1m/5m — AlphaESS's own Modbus spec recommends 5s+ |
| Inverter AC Power Limit | Dropdown: 3/4/4.6/5/6/8/10/12/15/20 kW (default `20`) — set this to your inverter's actual nameplate rating; caps how much power Force Charging/Discharging/Dispatch can request |
| Force Charging Power / Cutoff SoC / Duration | Defaults used by the Force Charging action when its own fields are left blank (kW / % / min) |
| Force Discharging Power / Cutoff SoC / Duration | Defaults used by the Force Discharging action when its own fields are left blank (kW / % / min) |
| Force Import Power / Cutoff SoC / Duration | Defaults used by the Force Import action when its own fields are left blank (kW target grid import / % / min) |

| State | Description |
|---|---|
| `loadPower` | Computed house load: `pvPower + batteryPower + gridPower` (W) |
| `invTemperature` | Inverter internal temperature (°C) |
| `invWorkMode` | Inverter operating mode (`Normal`, `Bypass/EPS`, or `Unknown work mode (N)` for any other model-specific code) |
| `systemTime` | Inverter's own clock, as reported by the inverter (`YYYY-MM-DD HH:MM:SS`) |
| `dispatchActive` | `true` while a Force Charging/Force Discharging/Force Import/Dispatch command is running |
| `dispatchType` | Which action started the active dispatch (`forceCharging`/`forceDischarging`/`forceImport`/`dispatch`) |
| `dispatchModeLabel` | The active dispatch's mode name (e.g. `State of Charge Control`) |
| `dispatchPowerTarget` | The active dispatch's power target — positive = discharging, negative = charging (W), same convention as `batteryPower` |
| `dispatchEndsAt` | When the active dispatch will auto-stop (`YYYY-MM-DD HH:MM:SS`), or blank if none is active |

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
| `batteryFull` | `true` when the BMS itself reports the battery as full |
| `batteryRemainingTime` | BMS estimate of time to full charge or empty, whichever direction the battery is currently going (min) |

A battery-less installation is currently **untested** — if you run one and see a `Modbus read error` on this device rather than sensible `0` values, please open an issue with your plugin log. It won't affect the Solar or Grid devices either way, since each is read and reported independently.

### AlphaESS Grid (`gridDevice`)

Auto-created under the Inverter.

| State | Description |
|---|---|
| `gridPower` | Total grid power — negative = exporting, positive = importing (W) |
| `gridPowerA` / `gridPowerB` / `gridPowerC` | Per-phase grid power (W) — on a single-phase installation, B/C simply read `0` |
| `gridVoltage` / `gridVoltageB` / `gridVoltageC` | Per-phase grid voltage (V) - if you have a SMILE-B3/SMILE-B3-PLUS inverter and this reads ~10x too low, [open an issue](https://github.com/coolcaper777/alphaess-modbus/issues); that model variant scales this register differently |
| `gridFrequency` | Grid frequency (Hz), read from the inverter's own frequency sensor |
| `lifetimeFeedToGrid` | Lifetime energy exported to grid (kWh) |
| `lifetimeConsumedFromGrid` | Lifetime energy imported from grid (kWh) |

Grid current isn't exposed - neither of the two independent, actively-maintained community register maps this plugin cross-checks against (see Credits) documents that register, so there's no verified source for its scale or reliability. It's derivable from `gridPower / gridVoltage` per phase if you need it.

The dashboard shows a per-phase Voltage/Power table automatically once it detects phase B or C carrying real voltage; a single-phase installation just sees the plain Voltage row it always has.

The dashboard also has a Dispatch detail card showing whether a dispatch is currently active and, if so, its type (Force Charging/Force Discharging/Dispatch), mode, power target, and when it auto-stops - plus a short "Dispatch: <type>" note in the header while one is running.

## Dispatch actions

Available as Indigo Actions on any **AlphaESS Inverter** device — usable in Action Groups, Schedules, and Trigger reactions. All five write AlphaESS's Dispatch registers, which are confirmed **not** flash-backed, so there's no wear concern from frequent use (unlike the inverter's own scheduler settings, which this plugin never touches).

- **Force Charging** / **Force Discharging** — charge or discharge the battery at a fixed power until a cutoff SoC or duration is reached, whichever comes first. Power/Cutoff SoC/Duration fields are optional per call — leave any blank to use that Force action's configured default on the Inverter device. Both always run under Dispatch Mode 2 (State of Charge Control) internally; there's no mode picker on these two, since that mode is what "fixed power + SoC target" *means* — any other mode would silently ignore one or both fields.
- **Force Import** — hold grid import at a fixed target level (e.g. topping up the battery from the grid during a cheap tariff window), charging the battery with whatever combination of that import and spare solar it takes. Unlike the other actions here, this isn't a single write: house load and solar shift constantly, so the plugin continuously re-corrects the setpoint against the actual measured grid power on every poll cycle (a proportional servo loop, tuned the same as the reference implementation's own), and stops automatically, early, once the battery settles near zero power flow — evidence the inverter's own SoC Control loop has already reached the cutoff — rather than waiting out the full configured duration.
- **Dispatch (Advanced)** — the general-purpose action, with a picker for all 8 documented Dispatch modes (Battery only Charges from PV, State of Charge Control, Load Following, Maximise Output, Normal Mode, Optimise Consumption, Maximise Consumption, No Battery Charge). The Power field only applies in modes 1/2/3/5 (the others are algorithm-driven and always run neutral); the Cutoff SoC field only applies in mode 2.
- **Dispatch Reset (Stop)** — stops whichever dispatch is currently active (including a running Force Import's servo loop) and returns the inverter to its normal scheduled operation.

A running dispatch auto-stops on its own once its duration elapses — no separate "stop" step needed unless you want to end it early. Starting a new dispatch (of any type) simply replaces whatever was active before, since the inverter only ever holds one dispatch configuration at a time.

**Validated ranges.** Every dispatch parameter is checked before anything is sent to the inverter — an out-of-range value logs an error and aborts the call rather than writing it. Cutoff SoC: 4–100%. Duration: 0–480 min (8 hours). Power: 0–20 kW for Force Charging/Discharging/Import, -20 to +20 kW for Dispatch (positive = discharge/export, negative = charge/import) — further capped to the Inverter AC Power Limit config field, so a call can never request more power than the inverter is actually rated for. These ranges are cross-checked against two independently maintained reference projects that agree exactly: [senalse/ha-alphaess-modbus](https://github.com/senalse/ha-alphaess-modbus)'s number-entity definitions and [Hillview Lodge](https://projects.hillviewlodge.ie/alphaess/)'s Home Assistant YAML integration.

**Not yet implemented:** Force Export and Excess Export. Like Force Import, both need a continuous servo loop against live PV/load/grid readings rather than a single fixed write, but on the export side of the meter instead — deferred to a future release.

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
- **Register map:** derived from [`SorX14/alphaess_modbus`](https://github.com/SorX14/alphaess_modbus) (MIT-licensed), cross-checked against [`senalse/ha-alphaess-modbus`](https://github.com/senalse/ha-alphaess-modbus) and the Home Assistant Modbus YAML at [projects.hillviewlodge.ie/alphaess](https://projects.hillviewlodge.ie/alphaess/) - the latter verified working against the user's own hardware.
- **Modbus library:** [`pymodbus`](https://github.com/pymodbus-dev/pymodbus).
- **Hardware:** [AlphaESS](https://www.alphaess.com/) hybrid inverters.
- **This plugin and its documentation** were built with [Claude Code](https://claude.com/claude-code) (Anthropic).
