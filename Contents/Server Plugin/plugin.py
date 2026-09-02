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

# Dispatch register block: 11 consecutive holding registers 0x0880-0x088A
# (2176-2186), written atomically via Modbus function code 16
# (write_registers) as one call. Confirmed NOT flash-backed - unlike the
# inverter's scheduler-config registers (max_feed_to_grid, charge/discharge
# cutoff SoC, charge/discharge time periods), which this plugin deliberately
# never writes. Source: https://projects.hillviewlodge.ie/alphaess/'s own
# author, in comments on that page: "Force Charging uses the Dispatch
# registers instead (which are not stored in the flash memory)." Register
# layout and the mode/scale constants below cross-checked against
# senalse/ha-alphaess-modbus's docs/register_map.md and switch.py/const.py.
# Defined here, ahead of REGISTERS below, because REGISTERS' own dispatch
# readback entries (dispatchModeRaw etc.) need DISPATCH_START_ADDRESS at
# module-load time, not just inside function bodies.
#
# Word layout (offset from DISPATCH_START_ADDRESS):
#   0    : Start (1=start, 0=stop)
#   1-2  : Active Power, 32-bit, 32000-biased (raw = 32000 - watts to charge,
#          32000 + watts to discharge, 32000 = neutral)
#   3-4  : Reactive Power, 32-bit, 32000 = neutral (always written neutral here)
#   5    : Mode - see DISPATCH_MODE_LABELS (defined further below)
#   6    : SoC target, raw = percent / DISPATCH_SOC_SCALE (only meaningful in
#          mode 2, State of Charge Control)
#   7-8  : Time, 32-bit, duration in seconds
#   9    : Flow Direction - always written as the constant DISPATCH_FLOW_DIRECTION
#   10   : PV Switch (0=unchanged, 1=on, 2=off) - only takes effect during an
#          active dispatch
DISPATCH_START_ADDRESS = 0x0880
# Corrected 2026-09-02 from 0.392 to 0.4 (raw = percent / 0.4, i.e. percent *
# 2.5) - the 0.392 value was carried forward from senalse/ha-alphaess-modbus
# (present unexplained since that project's very first commit, no rationale
# found in its history or in Hillview Lodge's page) without ever being
# checked against AlphaESS's own documentation. Two independent official
# AlphaESS sources agree on 0.4: the Household Modbus Register Parameter
# List's own worked example ("Send SOC=95, corresponding to the SOC of 38%"
# - 95 x 0.4 = 38) and the Modbus/Sever API Guide's explicit formula
# ("Para3 = SOC / 0.4"). With 0.392, a 90% cutoff request was actually being
# encoded as ~91.6% once decoded back - a small but real and entirely
# avoidable error. Not yet independently confirmed by a live test against
# this specific inverter's actual dispatch-stop behaviour (the official docs
# were considered authoritative enough on their own here, given the ~1.8%
# relative error the worked example above shows and the complete lack of
# any rationale for 0.392 anywhere).
DISPATCH_SOC_SCALE = 0.4
DISPATCH_MODE_SOC_CONTROL = 2
DISPATCH_FLOW_DIRECTION = 255
DISPATCH_PV_UNCHANGED = 0

# system_fault (0x08D4) sits far from every other cluster, so it's its own
# small extra round trip - defined here, ahead of REGISTERS below, for the
# same load-order reason as DISPATCH_START_ADDRESS above (REGISTERS'
# systemFaultRaw entry needs this name to already exist). inverter_warning_1/2,
# inverter_fault_1/2 (1078/1080/1082/1084) and battery_warning/battery_fault
# (284/286) don't need a cluster of their own - they already fall inside
# SOLAR_CLUSTER_START/BATTERY_CLUSTER_START's existing ranges (defined further
# below, but only referenced inside function bodies, not at module-load time,
# so their own later position is fine).
SYSTEM_FAULT_CLUSTER_START = 2260

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
    # Aggregate fault/warning bitmaps across all battery modules - source and
    # decode approach explained on systemFaultRaw below. Already inside this
    # cluster's own range (256-295), no extra Modbus round trip.
    "batteryWarningRaw": {"address": 284, "words": 2, "signed": False, "decimals": 0},
    "batteryFaultRaw": {"address": 286, "words": 2, "signed": False, "decimals": 0},

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
    # Inverter warning/fault bitmaps (one logical bitmap each, split across 2
    # uint32 words for Modbus width reasons per senalse/ha-alphaess-modbus's
    # register_map.md - not two separate categories). Already inside this
    # cluster's own range (1052-1088), no extra Modbus round trip. See
    # systemFaultRaw below for why these aren't decoded bit-by-bit.
    "inverterWarning1Raw": {"address": 1078, "words": 2, "signed": False, "decimals": 0},
    "inverterWarning2Raw": {"address": 1080, "words": 2, "signed": False, "decimals": 0},
    "inverterFault1Raw": {"address": 1082, "words": 2, "signed": False, "decimals": 0},
    "inverterFault2Raw": {"address": 1084, "words": 2, "signed": False, "decimals": 0},

    # System-level fault bitmap (cluster: 2260-2261, its own small extra round
    # trip - see SYSTEM_FAULT_CLUSTER_START). Condensed into systemHealthOK/
    # systemHealthDetail in _poll_inverter rather than exposed as 7 individual
    # near-permanently-zero device states: none of these bitmaps has a
    # documented per-bit meaning anywhere checked (Hillview Lodge's page,
    # senalse/ha-alphaess-modbus's register_map.md/const.py, or HA's own
    # entity attributes for these same registers) - they're only usable as
    # "zero = healthy, non-zero = something's wrong," even in the reference
    # implementations. The 12 per-module battery_N_warning/battery_N_fault
    # registers (0x0131-0x0147) are deliberately not read at all - disabled
    # by default even in senalse/ha-alphaess-modbus, and the aggregate
    # batteryWarningRaw/batteryFaultRaw above already cover "something's
    # wrong with a module" without needing per-module granularity.
    "systemFaultRaw": {"address": SYSTEM_FAULT_CLUSTER_START, "words": 2, "signed": False, "decimals": 0},

    # Dispatch block readback (cluster: 2176-2186, DISPATCH_START_ADDRESS aka
    # 0x0880) - the same 11 registers _write_dispatch writes, read back here
    # to reconcile dispatchActive/dispatchModeLabel/dispatchPowerTarget/
    # dispatchCutoffSoC against the inverter's actual state each poll, rather
    # than trusting only what this plugin last wrote (see the DISPATCH_MODE_*
    # decode block below _poll_inverter's other cluster handling). Added
    # 2026-09-01 after finding Home Assistant's own dispatch UI had gone
    # stale relative to the inverter (its input_select helpers reflect only
    # what HA last commanded, never read back) - this plugin had the exact
    # same latent gap on its dispatch* device states before this readback
    # was added. Not every word is decoded - Reactive Power (words 3-4) is
    # always written neutral and never meaningfully read; Flow Direction and
    # PV Switch (words 9-10) aren't currently surfaced as device states.
    "dispatchModeRaw": {"address": DISPATCH_START_ADDRESS + 5, "words": 1, "signed": False, "decimals": 0},
    "dispatchActivePowerRaw": {"address": DISPATCH_START_ADDRESS + 1, "words": 2, "signed": False, "decimals": 0},
    "dispatchSocRaw": {"address": DISPATCH_START_ADDRESS + 6, "words": 1, "signed": False, "decimals": 0},
}

# inverter_work_mode's documented meaning beyond these two codes is
# "model-specific" (per both the Hillview register docs and the
# senalse/ha-alphaess-modbus project) - anything else is shown as a raw
# fallback rather than guessed.
INVERTER_WORK_MODE_LABELS = {1: "Normal", 2: "Bypass/EPS"}

# Force Charging/Discharging always write mode 2 (SoC Control) - the reference
# implementation (senalse/ha-alphaess-modbus's switch.py) does the same for
# every one of its "Force" convenience switches, never exposing a mode picker
# on them: a fixed power target + SoC cutoff IS what SoC Control mode means,
# and every other mode would silently ignore one or both of those fields (see
# the mode-applicability comment on dispatch_action below). Mode choice is
# only exposed on the generic Dispatch action.
DISPATCH_MODE_LABELS = {
    1: "Battery only Charges from PV",
    2: "State of Charge Control",
    3: "Load Following",
    4: "Maximise Output",
    5: "Normal Mode",
    6: "Optimise Consumption",
    7: "Maximise Consumption",
    19: "No Battery Charge",
}

# Dispatch parameter valid ranges - cross-checked against two independently
# maintained sources that agree exactly: senalse/ha-alphaess-modbus's
# NUMBER_REGISTERS (custom_components/alphaess_modbus/const.py, the min/max
# declared on its HA number-entity sliders for these same dispatch params)
# and Hillview Lodge's integration_alpha_ess.yaml input_number helpers
# (https://projects.hillviewlodge.ie/alphaess/). Cutoff SoC's 4% floor is
# specific to the Dispatch block - the flash-backed scheduler's own charging
# cutoff SoC register uses a 10% floor instead, but this plugin never writes
# that register (see the DISPATCH_START_ADDRESS comment above).
DISPATCH_SOC_MIN = 4.0
DISPATCH_SOC_MAX = 100.0
DISPATCH_DURATION_MIN_MINUTES = 0.0
DISPATCH_DURATION_MAX_MINUTES = 480.0
# Absolute outer bound on Power regardless of inverter model - the real cap is
# whichever is smaller of this and the device's configured acLimitKw (see
# _resolve_power_limit_kw), matching both reference implementations' own
# ac_limit_scaled behavior for this same field.
DISPATCH_POWER_ABSOLUTE_MAX_KW = 20.0
DEFAULT_AC_LIMIT_KW = 20.0

# Force Import needs a standing servo loop, not a single write - the dispatch
# block only accepts a fixed charge-power word, but the actual goal (a fixed
# grid IMPORT level) shifts constantly with house load/PV. Tuning values below
# are taken directly from senalse/ha-alphaess-modbus's switch.py (proven in
# production against this same hardware family), not re-derived - see that
# source's own _SERVO_GAIN comment: a closed loop on the grid meter only
# (feeding back the calculated house-load estimate instead caused a kilowatt-
# scale limit cycle, since that estimate is itself derived from battery+grid),
# critically damped near gain 0.25, so 0.3 gives a fast, essentially
# overshoot-free response with zero steady-state error.
#
# Serviced once per poll cycle (_service_force_import, called at the end of
# _poll_inverter) rather than HA's ~2s coordinator cadence - this plugin has
# no equivalent fast tick tied to fresh Modbus reads, and a multi-hour grid
# top-up goal doesn't need 2-second responsiveness the way a zero-export
# tariff would. A slower servo just means slower convergence, not incorrect
# behavior.
FORCE_IMPORT_SERVO_GAIN = 0.3
FORCE_IMPORT_SERVO_DEADBAND_W = 80
FORCE_IMPORT_STALE_REWRITE_S = 60
# Battery power settling within this band for this long means the inverter's
# own SoC Control loop has already reached the cutoff and stopped moving
# power - stop early rather than waiting out the full configured duration.
FORCE_IMPORT_NEAR_ZERO_BAND_W = 50
FORCE_IMPORT_NEAR_ZERO_HOLD_S = 10

# Pause/Resume safety mechanism, ported from Hillview Lodge's
# integration_alpha_ess.yaml (AlphaESS_Force_Import_Pause/_Resume, read in
# full before implementing) - a real gap senalse/ha-alphaess-modbus's version
# doesn't have at all. Pauses (writes a neutral dispatch, but keeps Force
# Import logically active - see _service_force_import) if the target can't
# actually be met without draining the battery, if PV production is clipping
# against the inverter's AC limit, or if the inverter leaves Normal work mode
# at all (e.g. a grid-outage Bypass/EPS event - that condition in the source
# reads like it came from a real incident). Only resumes once work mode has
# been confirmed Normal for a full 10 minutes AND the overload has cleared -
# deliberately conservative, not a quick bounce-back.
FORCE_IMPORT_OVERLOAD_HOLD_S = 5
FORCE_IMPORT_RESUME_NORMAL_HOLD_S = 600
# Every one of the *_HOLD_S constants above is a wall-clock duration, but this
# plugin only samples once per poll (pollInterval, user-configurable 10-300s)
# - unlike HA's ~2s coordinator tick, which is fast enough that a single poll
# gap is never a concern. Checking elapsed time alone breaks down for a slow
# poller: if pollInterval is 300s and a HOLD_S of 10 is checked purely by
# wall-clock, the *very first* observation already satisfies "10 seconds have
# elapsed since I started tracking this" as soon as a second poll arrives -
# nothing was actually confirmed "sustained," just two samples 300s apart
# that both happened to qualify. _track_sustained (used by all three HOLD_S
# checks below) additionally requires this many consecutive qualifying polls,
# regardless of how much wall-clock time that represents - the honest
# statement of what a slow poller can actually confirm, rather than a
# threshold whose real meaning silently depends on a poll rate nobody chose
# with this constant in mind.
FORCE_IMPORT_MIN_SUSTAINED_POLLS = 2

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
INVERTER_STATE_KEYS = [
    "loadPower", "invTemperature", "invWorkMode", "systemTime",
    "dispatchActive", "dispatchType", "dispatchModeLabel", "dispatchPowerTarget", "dispatchEndsAt",
    "dispatchCutoffSoC", "systemHealthOK", "systemHealthDetail",
]
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
    (DISPATCH_START_ADDRESS, 11),  # 2176-2186: dispatch block readback - see the REGISTERS comment above
    (SYSTEM_FAULT_CLUSTER_START, 2),  # 2260-2261: system_fault only - see the REGISTERS comment on systemFaultRaw
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
  .dot-neutral { background: var(--text-muted); }
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
    <div class="detail-card">
      <h2><span class="dot dot-neutral" style="display:inline-block;"></span> Dispatch</h2>
      <div class="rows" id="dispatchDetail"><span class="empty-note">No dispatch currently active</span></div>
    </div>
  </div>

  <footer>Auto-refreshes every 5 seconds.</footer>
</div>
<script>
(function () {
  var POLL_MS = 5000;
  var selectedDeviceId = null;
  var DISPATCH_TYPE_LABELS = {forceCharging: "Force Charging", forceDischarging: "Force Discharging", dispatch: "Dispatch"};
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

  function renderDispatchDetail(inverter) {
    var container = document.getElementById("dispatchDetail");
    container.replaceChildren();
    if (!inverter || !inverter.dispatchActive) {
      container.appendChild(Object.assign(document.createElement("span"), {className: "empty-note", textContent: "No dispatch currently active"}));
      return;
    }
    container.appendChild(row("Type", DISPATCH_TYPE_LABELS[inverter.dispatchType] || inverter.dispatchType || "-"));
    container.appendChild(row("Mode", inverter.dispatchModeLabel || "-"));
    var pw = inverter.dispatchPowerTarget || 0;
    container.appendChild(row("Power target", pw === 0 ? "Neutral"
      : (pw > 0 ? "\u2193 Discharging \u00b7 " + formatPower(pw) : "\u2191 Charging \u00b7 " + formatPower(Math.abs(pw)))));
    if (inverter.dispatchCutoffSoC) {
      container.appendChild(row("Cutoff SoC", fmt(inverter.dispatchCutoffSoC, 1, "%")));
    }
    container.appendChild(row("Ends at", inverter.dispatchEndsAt || "-"));
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
      if (data.inverter.dispatchActive) {
        metaParts.push("Dispatch: " + (DISPATCH_TYPE_LABELS[data.inverter.dispatchType] || data.inverter.dispatchType || "active"));
      }
      if (data.inverter.systemHealthOK === false) {
        metaParts.push("\u26a0 " + (data.inverter.systemHealthDetail || "Fault/warning active"));
      }
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
    renderDispatchDetail(data.inverter);

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


# Bit-name lookups for systemHealthDetail's fault/warning decode in
# _poll_inverter - sourced from AlphaESS's own official "Household Modbus
# Register Parameter List" PDF (Note4/Note6/Note26/Note28/Note32), not
# reverse-engineered. That PDF isn't linked directly from AlphaESS's own
# site but is mirrored at https://projects.hillviewlodge.ie/_alphaess/ -
# fetching it needs a Referer header set to that site or the host 403s a
# direct request. INVERTER_FAULT1_BITS/INVERTER_FAULT2_BITS and
# INVERTER_WARNING1_BITS/INVERTER_WARNING2_BITS are exact - their source
# table's two columns are two actual separate registers (Fault1 vs Fault2,
# Warning1 vs Warning2), not ambiguous. SYSTEM_FAULT_BITS and
# BATTERY_FAULT_BITS are best-effort: their source table has two columns for
# different EMS firmware platforms (EMS2.5 vs EMS3.5/EMS3.6, or a similar
# platform split) that couldn't be reliably attributed per-bit from the
# PDF's flattened text extraction - where the two differed, both are joined
# with " / " rather than guessing which applies to this specific inverter's
# firmware. Bits with no name in the source table (reserved/undefined) fall
# back to "bit N" in _decode_fault_bits rather than being silently dropped.
SYSTEM_FAULT_BITS = {
    0: "Network_Card_Fault", 1: "Rtc_Fault", 2: "EEprom_Fault", 3: "INV_Comms_Error",
    4: "Grid_Meter_Lost", 5: "PV_Meter_Lost / Meter Not Set", 6: "BMS_Lost",
    7: "UPS_Battery_Volt_Low / SD not inserted or SD write error", 8: "Backup_Overload",
    9: "INV_Slave_Lost", 10: "INV_Master_Lost", 11: "Parallel_Comm_Error",
    12: "Parallel_Mode_Differ", 13: "Flash_Fault", 14: "SDRAM error",
    15: "Extension CAN error", 16: "inv type not specified", 18: "DG_PV_Conflict",
    19: "PV_INV_Fault", 20: "AirConFault", 23: "GC_Fault", 25: "OverCurr",
    26: "PcsModeFault", 27: "BatEnergyLow",
}
BATTERY_FAULT_BITS = {
    0: "Temperature sensor error", 1: "Mos error", 2: "Cell Temp Differ / Circuit breaker open",
    3: "Balancer Fault / Dial switching mode inconsistence",
    4: "Charge Over Current / Slave battery communication lost",
    5: "Balancer Mos Fault / Sn missing",
    6: "Discharge Over Current / Master battery communication lost",
    7: "Pole Over Temp / Firmware versions inconsistence",
    8: "Cell Over Volt / Multi master error",
    9: "Cell Volt Differ / Mos high temperature",
    10: "Discharge Low Temp / Insulation fault",
    11: "Total pressure abnormal",
    12: "Cell Low Volt / Mos feedback failure",
    13: "ISO Comm Fault / Prefilled failure",
    14: "LMU SN Repeat / 17823 communication failure",
    15: "17841 communication failure",
    16: "IR Fault / Mos temperature sensor error",
    17: "LMU Comm Fault", 18: "Cell Over Temp", 19: "BMU Comm Fault",
    21: "Charge Low Temp", 23: "Volt Detect Fault", 24: "Wire Harness Fault",
    26: "Relay Fault", 27: "LMU ID Repeat", 28: "LMU ID Discontinuous",
    29: "Current Sensor Fault", 31: "Temp Sensor Fault",
}
BATTERY_WARNING_BITS = {
    0: "Temperature imbalance", 1: "Over temperature", 2: "Discharge low temperature",
    3: "Charge low temperature", 4: "Discharge over current", 5: "Charge over current",
    6: "Cell over voltage", 7: "Cell low voltage", 8: "sw_inconsistence",
    9: "mos_temperature_sensor_error", 10: "soc_inconsistence", 11: "bms_sci_lost",
    12: "bms_fan_err",
}
INVERTER_FAULT1_BITS = {
    0: "Grid_OVP", 1: "Grid_UVP", 2: "Grid_OFP", 3: "Grid_UFP", 4: "phase_locked_fault",
    5: "bus_ovp1", 6: "bus_ovp2", 7: "insulation_fault", 8: "gfci_fault", 9: "gfci_test_fault",
    10: "grid_relay_fault", 11: "over_temperature", 12: "pv_reverse", 13: "bat_reverse",
    14: "m_s_com_fault", 15: "display_com_fault", 16: "chip1_upgrade_fault", 17: "mppt1_ovp",
    18: "mppt1_sw_ocp", 19: "mppt1_hw_ocp", 20: "mppt1_otp", 21: "mppt2_ovp", 22: "mppt2_sw_ocp",
    23: "mppt2_hw_ocp", 24: "mppt2_otp", 25: "bat_ovp", 26: "bat_uvp", 27: "battery_lose",
    28: "bat_otp", 29: "bat1_charge_ocp", 30: "bat1_discharge_ocp", 31: "bat2_charge_ocp",
}
INVERTER_FAULT2_BITS = {
    0: "bat2_discharge_ocp", 1: "bat1_hw_ocp", 2: "bat2_hw_ocp", 3: "inv_otp", 4: "inv_ovp",
    5: "inv_uvp", 6: "output_dc_over_current", 7: "inv_ocp", 8: "inv_hw_ocp",
    9: "output_dc_over_voltage", 10: "output_short", 11: "output_overload", 12: "apu_uvp",
    13: "bat_relay_fault", 14: "dc_input_disturbance", 15: "grid_disturbance",
    16: "gird_unbalance", 17: "freq_jitter", 18: "grid_overcurrent",
    19: "grid_current_track_fault", 20: "backup_ovp", 21: "dc_bus_unbalancevolt",
    22: "dc_bus_undervolt", 23: "dc_bus_unbalancevolt2", 24: "igbt_over_current",
    25: "grid_disturbance2", 26: "afci_check_protect", 27: "grid_current_sampling_abnormal",
    28: "dsp_selfcheck", 29: "grid_short_time_over_current", 30: "bat_overvolt_hardware_fault",
    31: "zero_ground_fault",
}
INVERTER_WARNING1_BITS = {
    0: "bat_over_voltage_alarm", 1: "bat_under_voltage_alarm", 2: "output_overload_alarm",
    3: "abnormal_temperature_sensor", 4: "dc_power_alarm", 5: "battery_stops_running_alarm",
    6: "over_temperature_alarm", 7: "pv_volt_high_alarm", 8: "bat_open_alarm",
    9: "bat_reverse_alarm", 10: "bus_over_alarm", 11: "grid_loss_alarm", 12: "grid_volt_alarm",
    13: "grid_freq_alarm", 14: "10min_grid_volt_alarm", 15: "grid_volt_inst_over",
    16: "pe_loss_alarm", 17: "ln_reverse", 18: "low_temper_alarm", 19: "gfci_alarm",
    20: "iso_alarm", 21: "dci_alarm", 22: "dcv_alarm", 23: "island_alarm",
    24: "fan_abnormal_alarm", 25: "n_loss_alarm", 26: "ems_sci_alarm", 27: "ems_can_alarm",
    28: "flashid_alarm", 29: "read_flash_alarm", 30: "write_flash_alarm",
    31: "machine_type_alarm",
}
INVERTER_WARNING2_BITS = {
    0: "inv_volt_low_alarm", 1: "inv_over_curr_sw_alarm", 2: "inv_over_curr_hw_alarm",
    3: "bst_over_curr_sw_alarm", 4: "bst_over_curr_hw_alarm", 5: "buck_bst_over_curr_sw_alarm",
    6: "buck_bst_over_curr_hw_alarm", 7: "bus_under", 8: "no_pv_input_alarm",
    9: "input_power_limit_alarm", 10: "output_power_limit_alarm",
    11: "reduce_pby_over_freq_alarm", 12: "reduce_pby_over_volt_alarm",
    13: "reduce_pby_over_temp_alarm", 14: "hvrt_alarm", 15: "lvrt_alarm", 16: "ntc_fail_alarm",
    17: "grid_waveform_abnormal_alarm", 18: "eps_capacitance_decrease", 19: "para_alarm",
    20: "para_error_location", 21: "para_avg_overload", 22: "para_module_addr_same",
    23: "para_online_enter_fail", 24: "para_unbalance_power", 25: "para_turnon_inconsistent",
    26: "grid_backup_n_lost", 27: "bat_num_abnormal", 28: "grid_phase_order_fault",
    29: "dcv_sample_abnormal", 30: "blackbox_flash_fault", 31: "rtc_fault",
}


def _decode_fault_bits(raw: int, bit_names: dict) -> str:
    """Decode a 32-bit fault/warning bitmap into a comma-joined list of set bit names.

    Args:
        raw (int): The raw uint32 bitmap value.
        bit_names (dict): bit index -> name, one of the *_BITS lookups above.

    Returns:
        str: Comma-joined names of every set bit, e.g. "Battery Fault=4"
            decodes to "Charge Over Current / Slave battery communication
            lost". A set bit with no entry in bit_names (reserved/undefined
            in the source table) falls back to "bit N" rather than being
            silently dropped.
    """
    return ", ".join(bit_names.get(i, f"bit {i}") for i in range(32) if raw & (1 << i))


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


class _MenuPluginAction:
    """Minimal stand-in for indigo.PluginAction, for MenuItems.xml callbacks
    that call an existing Actions.xml callback directly with no per-call
    field overrides. Supplies just the two attributes those callbacks read -
    deviceId and an empty props dict - so every one of that Action's own
    overridable fields resolves straight through to the device's configured
    defaults, exactly as if a real Action Group step had left them all blank.
    """

    def __init__(self, device_id: int):
        self.deviceId = device_id
        self.props: dict = {}


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
        # inverter device id -> epoch time an active dispatch should auto-stop.
        # Checked on runConcurrentThread's 5-second tick (independent of each
        # device's own, possibly slower pollInterval), mirroring the reference
        # HA implementation's in-memory auto-off timer without needing
        # Indigo-side scheduling.
        self._dispatch_end_at: dict = {}
        # inverter device id -> Force Import servo state (target_import_w,
        # soc_raw, duration_s, power_limit_w, last_power_raw, last_write_time,
        # near_zero_since). Populated by force_import_action, serviced once
        # per poll by _service_force_import (called from _poll_inverter),
        # cleared in _stop_dispatch so every stop path (explicit reset,
        # duration auto-off, or the servo's own early-stop) tears it down.
        self._force_import_state: dict = {}

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
            # Same ranges _validate_dispatch_number enforces at Action time (see that
            # method's docstring for the source) - checked here too so a bad default
            # is rejected right in this dialog instead of only when an Action actually
            # uses it. acLimitKw is itself a fixed-option menu, so it needs no range
            # check - only used here to size the two Power fields' upper bound.
            try:
                ac_limit_kw = float(valuesDict.get("acLimitKw", DEFAULT_AC_LIMIT_KW))
            except (TypeError, ValueError):
                ac_limit_kw = DEFAULT_AC_LIMIT_KW
            power_limit_kw = min(DISPATCH_POWER_ABSOLUTE_MAX_KW, ac_limit_kw)
            self._validate_config_number(errors_dict, valuesDict, "forceChargingPower", "Force Charging Power (kW)", 0.0, power_limit_kw)
            self._validate_config_number(errors_dict, valuesDict, "forceChargingCutoffSoC", "Force Charging Cutoff SoC (%)", DISPATCH_SOC_MIN, DISPATCH_SOC_MAX)
            self._validate_config_number(errors_dict, valuesDict, "forceChargingDuration", "Force Charging Duration (min)", DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES)
            self._validate_config_number(errors_dict, valuesDict, "forceDischargingPower", "Force Discharging Power (kW)", 0.0, power_limit_kw)
            self._validate_config_number(errors_dict, valuesDict, "forceDischargingCutoffSoC", "Force Discharging Cutoff SoC (%)", DISPATCH_SOC_MIN, DISPATCH_SOC_MAX)
            self._validate_config_number(errors_dict, valuesDict, "forceDischargingDuration", "Force Discharging Duration (min)", DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES)
            self._validate_config_number(errors_dict, valuesDict, "forceImportPower", "Force Import Power (kW)", 0.0, power_limit_kw)
            self._validate_config_number(errors_dict, valuesDict, "forceImportCutoffSoC", "Force Import Cutoff SoC (%)", DISPATCH_SOC_MIN, DISPATCH_SOC_MAX)
            self._validate_config_number(errors_dict, valuesDict, "forceImportDuration", "Force Import Duration (min)", DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES)
        elif typeId in ("solarDevice", "batteryDevice", "gridDevice"):
            if not valuesDict.get("systemDevice", ""):
                errors_dict["systemDevice"] = "Please select the AlphaESS Inverter this device belongs to."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    @staticmethod
    def _validate_config_number(errors_dict: indigo.Dict, valuesDict: indigo.Dict, field_id: str, label: str, min_v: float, max_v: float) -> None:
        """Validate one numeric Device Edit dialog field is present, numeric, and in range.

        Args:
            errors_dict (indigo.Dict): The dialog's error dict - mutated in place,
                setting ``errors_dict[field_id]`` on failure.
            valuesDict (indigo.Dict): The dialog's current field values.
            field_id (str): The field's id in Devices.xml.
            label (str): Human-readable field name for the error message.
            min_v (float): Minimum valid value, inclusive.
            max_v (float): Maximum valid value, inclusive.
        """
        text = valuesDict.get(field_id, "").strip()
        try:
            value = float(text)
        except ValueError:
            errors_dict[field_id] = f"{label} must be a number."
            return
        if not (min_v <= value <= max_v):
            errors_dict[field_id] = f"{label} must be between {min_v} and {max_v}."

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

                # Auto-off for any active dispatch whose duration has expired -
                # checked every 5s regardless of each device's own pollInterval,
                # since this is independent of register reads.
                for dev_id, end_at in list(self._dispatch_end_at.items()):
                    if now < end_at:
                        continue
                    if dev_id not in indigo.devices:
                        self._dispatch_end_at.pop(dev_id, None)
                        continue
                    try:
                        self._stop_dispatch(indigo.devices[dev_id], reason="duration expired")
                    except Exception:
                        self.logger.exception(f"Error auto-stopping dispatch for device {dev_id}")

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
        self._dispatch_end_at.pop(dev.id, None)
        self._force_import_state.pop(dev.id, None)

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
        work_mode = int(values["invWorkMode"]) if solar_ok else None
        if solar_ok:
            inverter_states.append({"key": "invTemperature", "value": values["invTemperature"], "decimalPlaces": 1})
            inverter_states.append({
                "key": "invWorkMode",
                "value": INVERTER_WORK_MODE_LABELS.get(work_mode, f"Unknown work mode ({work_mode})"),
            })
        if time_ok:
            inverter_states.append({"key": "systemTime", "value": _decode_system_time(cluster_registers[SYSTEM_TIME_CLUSTER_START])})

        # Reconcile dispatch* states against the inverter's own readback
        # every poll, rather than trusting only what _start_dispatch/
        # _stop_dispatch last wrote - see the REGISTERS comment on
        # dispatchModeRaw for why. mode 0 means no dispatch is active
        # (matches every "nothing engaged" reading seen against this
        # hardware). dispatchType/dispatchEndsAt have no register
        # equivalent - the inverter doesn't track which Indigo action
        # started a dispatch or expose a remaining-time countdown (the Time
        # word reads back as the configured duration, not a countdown, per
        # direct observation), so those two stay write-side/informational,
        # only cleared here when readback confirms nothing is active.
        dispatch_ok = DISPATCH_START_ADDRESS in cluster_registers
        if dispatch_ok:
            dispatch_mode_raw = int(values["dispatchModeRaw"])
            dispatch_active = dispatch_mode_raw != 0
            dispatch_power_w = int(values["dispatchActivePowerRaw"]) - 32000
            cutoff_soc = (
                values["dispatchSocRaw"] * DISPATCH_SOC_SCALE
                if dispatch_active and dispatch_mode_raw == DISPATCH_MODE_SOC_CONTROL else 0.0
            )
            inverter_states.append({"key": "dispatchActive", "value": dispatch_active})
            inverter_states.append({
                "key": "dispatchModeLabel",
                "value": DISPATCH_MODE_LABELS.get(dispatch_mode_raw, f"Unknown mode ({dispatch_mode_raw})") if dispatch_active else "",
            })
            inverter_states.append({"key": "dispatchPowerTarget", "value": dispatch_power_w if dispatch_active else 0})
            inverter_states.append({"key": "dispatchCutoffSoC", "value": round(cutoff_soc, 1), "decimalPlaces": 1})
            if not dispatch_active:
                inverter_states.append({"key": "dispatchType", "value": ""})
                inverter_states.append({"key": "dispatchEndsAt", "value": ""})

        # Condensed fault/warning health check - see the REGISTERS comment on
        # systemFaultRaw for why this is one boolean + a detail string rather
        # than 7 individual near-permanently-zero device states, and the
        # comment above _decode_fault_bits for where the bit-name decode
        # comes from. Only set when every one of the three relevant clusters
        # actually read this cycle - a partial read shouldn't produce a
        # false "all clear" from incomplete data, same posture as loadPower
        # below.
        fault_ok = SYSTEM_FAULT_CLUSTER_START in cluster_registers
        if solar_ok and battery_ok and fault_ok:
            problems = []
            for label, key, bit_names in (
                ("System Fault", "systemFaultRaw", SYSTEM_FAULT_BITS),
                ("Inverter Warning 1", "inverterWarning1Raw", INVERTER_WARNING1_BITS),
                ("Inverter Warning 2", "inverterWarning2Raw", INVERTER_WARNING2_BITS),
                ("Inverter Fault 1", "inverterFault1Raw", INVERTER_FAULT1_BITS),
                ("Inverter Fault 2", "inverterFault2Raw", INVERTER_FAULT2_BITS),
                ("Battery Warning", "batteryWarningRaw", BATTERY_WARNING_BITS),
                ("Battery Fault", "batteryFaultRaw", BATTERY_FAULT_BITS),
            ):
                raw = int(values[key])
                if raw:
                    problems.append(f"{label}: {_decode_fault_bits(raw, bit_names)}")
            inverter_states.append({"key": "systemHealthOK", "value": len(problems) == 0})
            inverter_states.append({"key": "systemHealthDetail", "value": "; ".join(problems)})

        load_power = None
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

        # One consolidated line per poll with every figure that mattered for
        # reconstructing dispatch behaviour after the fact when troubleshooting
        # against Home Assistant's history/logbook (2026-08-31/09-01) - grep the
        # Indigo log for "snapshot" with debug logging on for the same kind of
        # grid/battery/PV/load/SoC/mode trace that investigation needed, instead
        # of wading through the raw per-register dump above.
        mode_txt = INVERTER_WORK_MODE_LABELS.get(work_mode, f"Unknown ({work_mode})") if work_mode is not None else "?"
        soc_txt = f"{values['batterySoC']:.1f}%" if battery_ok else "?"
        batt_txt = f"{values['batteryPower']:.0f}W" if battery_ok else "?"
        grid_txt = f"{values['gridPower']:.0f}W" if grid_ok else "?"
        pv_txt = f"{pv_power:.0f}W" if pv_power is not None else "?"
        load_txt = f"{load_power:.0f}W" if load_power is not None else "?"
        dispatch_txt = dev.states.get("dispatchModeLabel") or "none"
        self.logger.debug(
            f"{dev.name}: snapshot - workMode={mode_txt} dispatch={dispatch_txt} "
            f"SoC={soc_txt} battery={batt_txt} grid={grid_txt} pv={pv_txt} load={load_txt}"
        )

        if dev.id in self._force_import_state:
            self._service_force_import(
                dev,
                grid_ok=grid_ok,
                grid_power=values.get("gridPower") if grid_ok else None,
                battery_ok=battery_ok,
                battery_power=values.get("batteryPower") if battery_ok else None,
                solar_ok=solar_ok,
                work_mode=work_mode,
                pv_power=pv_power,
                load_power=load_power,
            )

    def _open_dispatch_client(self, dev: indigo.Device) -> Optional[tuple]:
        """Connect a Modbus client for a dispatch write against an inverter device.

        Mirrors the connect logic in ``_poll_inverter`` but returns instead of
        setting the device's own error state - a dispatch command failing to
        connect shouldn't overwrite whatever error/OK state the last register
        poll left in place.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device the action targets.

        Returns:
            Optional[tuple]: ``(client, unit_id)`` if connected, or None (with
                the failure already logged) on any config/connection failure.
        """
        address = dev.pluginProps.get("address", "")
        if not address:
            self.logger.error(f"{dev.name}: No IP address configured - cannot send dispatch command")
            return None
        try:
            port = int(dev.pluginProps.get("port", 502))
            unit_id = int(dev.pluginProps.get("unitId", DEFAULT_UNIT_ID))
        except (TypeError, ValueError):
            self.logger.error(f"{dev.name}: Invalid Port or Unit ID - cannot send dispatch command")
            return None
        client = ModbusTcpClient(address, port=port, timeout=5)
        if not client.connect():
            self.logger.error(f"{dev.name}: could not connect to {address}:{port} - cannot send dispatch command")
            return None
        return client, unit_id

    @staticmethod
    def _resolve_number(override, device_default, fallback: float) -> float:
        """Resolve an optional Action field, falling back to a device default, then a hardcoded fallback.

        Args:
            override: The Action instance's own field value (may be blank/None).
            device_default: The inverter device's configured default for this field (may be blank/None).
            fallback (float): Used only if both override and device_default are blank/invalid.

        Returns:
            float: The resolved numeric value.
        """
        for raw in (override, device_default):
            text = (raw or "").strip()
            if text:
                try:
                    return float(text)
                except ValueError:
                    continue
        return fallback

    def _resolve_power_limit_kw(self, dev: indigo.Device) -> float:
        """Resolve the inverter's configured AC power limit, in kW.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device.

        Returns:
            float: The device's ``acLimitKw`` config value, or
                ``DEFAULT_AC_LIMIT_KW`` if unset/invalid (e.g. on a device
                created before this field existed, until it's re-saved).
        """
        try:
            return float(dev.pluginProps.get("acLimitKw", DEFAULT_AC_LIMIT_KW))
        except (TypeError, ValueError):
            return DEFAULT_AC_LIMIT_KW

    def _validate_dispatch_number(self, dev: indigo.Device, label: str, value: float, min_v: float, max_v: float) -> bool:
        """Reject an out-of-range dispatch parameter rather than sending it to the inverter.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device (for the log line).
            label (str): Human-readable field name, e.g. "Force Charging Power (kW)".
            value (float): The resolved value to check.
            min_v (float): Minimum valid value, inclusive.
            max_v (float): Maximum valid value, inclusive.

        Returns:
            bool: True if ``value`` is within ``[min_v, max_v]``. False (with
                an error already logged) otherwise - the caller should abort
                the dispatch write without contacting the inverter.
        """
        if min_v <= value <= max_v:
            return True
        self.logger.error(f"{dev.name}: {label} of {value} is out of range ({min_v}-{max_v}) - dispatch not sent")
        return False

    def _write_dispatch(self, client: ModbusTcpClient, unit_id: int, *, power_raw: int, mode: int,
                         soc_raw: int, duration_s: int, pv_switch: int = DISPATCH_PV_UNCHANGED) -> None:
        """Write the 11-register Dispatch block (DISPATCH_START_ADDRESS) as one atomic write.

        Not flash-backed - safe to write as often as needed (see the
        DISPATCH_START_ADDRESS comment above for the source confirming this).

        Args:
            client (ModbusTcpClient): An already-connected Modbus client.
            unit_id (int): The inverter's Modbus unit/slave ID.
            power_raw (int): 32000-biased active power word (<32000 charges, >32000 discharges).
            mode (int): Dispatch mode code - see DISPATCH_MODE_LABELS.
            soc_raw (int): SoC target word (percent / DISPATCH_SOC_SCALE) - only meaningful in mode 2.
            duration_s (int): Dispatch duration in seconds.
            pv_switch (int): 0=unchanged, 1=PV on, 2=PV off - only takes effect during an active dispatch.

        Raises:
            ModbusException: If the write itself reports an error.
        """
        values = [1, 0, power_raw, 0, 32000, mode, soc_raw, 0, duration_s, DISPATCH_FLOW_DIRECTION, pv_switch]
        result = client.write_registers(DISPATCH_START_ADDRESS, values, device_id=unit_id)
        if result.isError():
            raise ModbusException(f"error writing dispatch registers: {result}")

    def _start_dispatch(self, dev: indigo.Device, *, dispatch_type: str, power_raw: int, mode: int,
                         soc_raw: int, duration_s: int, dispatch_power_w: int,
                         pv_switch: int = DISPATCH_PV_UNCHANGED) -> bool:
        """Write a Dispatch command and update the inverter device's dispatch states.

        Shared by force_charging_action/force_discharging_action/dispatch_action/
        force_import_action. Mutual exclusivity between dispatch types needs no
        special handling here: the inverter only ever holds one dispatch config
        on the wire, so starting a new one already supersedes whatever was
        active before - this just overwrites the tracked state to match.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device to dispatch on.
            dispatch_type (str): Which action started this (e.g. "forceCharging").
            power_raw (int): 32000-biased active power word.
            mode (int): Dispatch mode code.
            soc_raw (int): SoC target word.
            duration_s (int): Dispatch duration in seconds.
            dispatch_power_w (int): Signed watts for the dispatchPowerTarget state
                (positive=discharging, negative=charging - same convention as
                the batteryPower state).
            pv_switch (int): 0=unchanged, 1=PV on, 2=PV off.

        Returns:
            bool: True if the write succeeded and device states were updated,
                False on any connection/write failure (already logged) - used
                by force_import_action to avoid tracking servo state for a
                dispatch that was never actually established.
        """
        opened = self._open_dispatch_client(dev)
        if opened is None:
            return False
        client, unit_id = opened
        try:
            self._write_dispatch(client, unit_id, power_raw=power_raw, mode=mode, soc_raw=soc_raw,
                                  duration_s=duration_s, pv_switch=pv_switch)
        except ModbusException:
            self.logger.exception(f"{dev.name}: error writing dispatch command")
            return False
        finally:
            client.close()

        end_at = time.time() + duration_s
        self._dispatch_end_at[dev.id] = end_at
        mode_label = DISPATCH_MODE_LABELS.get(mode, f"Unknown mode ({mode})")
        cutoff_pct = soc_raw * DISPATCH_SOC_SCALE if mode == DISPATCH_MODE_SOC_CONTROL else 0.0
        # Optimistic feedback, immediately - _poll_inverter's dispatch-block
        # readback (see the REGISTERS comment on dispatchModeRaw) reconciles
        # these against the inverter's actual state on the very next poll
        # regardless, so a momentarily-optimistic value here is harmless.
        dev.updateStatesOnServer([
            {"key": "dispatchActive", "value": True},
            {"key": "dispatchType", "value": dispatch_type},
            {"key": "dispatchModeLabel", "value": mode_label},
            {"key": "dispatchPowerTarget", "value": dispatch_power_w},
            {"key": "dispatchEndsAt", "value": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_at))},
            {"key": "dispatchCutoffSoC", "value": round(cutoff_pct, 1), "decimalPlaces": 1},
        ])
        cutoff_txt = f"{cutoff_pct:.1f}% (raw soc={soc_raw})" if mode == DISPATCH_MODE_SOC_CONTROL else "n/a for this mode"
        self.logger.info(
            f"{dev.name}: Dispatch started - type={dispatch_type}, mode={mode_label}, "
            f"power={dispatch_power_w}W, cutoffSoC={cutoff_txt}, duration={duration_s}s"
        )
        return True

    def _stop_dispatch(self, dev: indigo.Device, *, reason: str) -> None:
        """Write the Dispatch stop command (word 0 = 0) and clear the device's dispatch states.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device to stop dispatch on.
            reason (str): Human-readable reason, logged (e.g. "user requested" or "duration expired").
        """
        self._dispatch_end_at.pop(dev.id, None)
        self._force_import_state.pop(dev.id, None)
        opened = self._open_dispatch_client(dev)
        if opened is None:
            return
        client, unit_id = opened
        try:
            result = client.write_registers(DISPATCH_START_ADDRESS, [0] * 11, device_id=unit_id)
            if result.isError():
                raise ModbusException(f"error writing dispatch stop: {result}")
        except ModbusException:
            self.logger.exception(f"{dev.name}: error stopping dispatch")
            return
        finally:
            client.close()

        self.logger.info(f"{dev.name}: Dispatch stopped ({reason})")
        dev.updateStatesOnServer([
            {"key": "dispatchActive", "value": False},
            {"key": "dispatchType", "value": ""},
            {"key": "dispatchModeLabel", "value": ""},
            {"key": "dispatchPowerTarget", "value": 0},
            {"key": "dispatchEndsAt", "value": ""},
            {"key": "dispatchCutoffSoC", "value": 0.0, "decimalPlaces": 1},
        ])

    @staticmethod
    def _track_sustained(state: dict, since_key: str, count_key: str, condition_now: bool,
                          hold_s: float, min_polls: int = FORCE_IMPORT_MIN_SUSTAINED_POLLS) -> bool:
        """Track whether a condition has held across polls, resetting whenever it goes false.

        Requires BOTH ``hold_s`` of elapsed wall-clock time AND ``min_polls``
        consecutive qualifying polls - see the FORCE_IMPORT_MIN_SUSTAINED_POLLS
        comment for why elapsed time alone isn't a meaningful "sustained" check
        when pollInterval can be longer than hold_s.

        Args:
            state (dict): The tracking dict to read/update (e.g. one inverter's
                entry in self._force_import_state).
            since_key (str): Key holding the epoch time the condition first
                became true (None if not currently tracking).
            count_key (str): Key holding the number of consecutive polls the
                condition has been true.
            condition_now (bool): Whether the condition is true this poll.
            hold_s (float): Minimum elapsed wall-clock time required.
            min_polls (int): Minimum consecutive qualifying polls required.

        Returns:
            bool: True once both thresholds are satisfied.
        """
        if not condition_now:
            state[since_key] = None
            state[count_key] = 0
            return False
        now = time.time()
        if state[since_key] is None:
            state[since_key] = now
            state[count_key] = 1
            return False
        state[count_key] += 1
        return (now - state[since_key] >= hold_s) and (state[count_key] >= min_polls)

    def _service_force_import(self, dev: indigo.Device, *, grid_ok: bool, grid_power: Optional[float],
                               battery_ok: bool, battery_power: Optional[float], solar_ok: bool,
                               work_mode: Optional[int], pv_power: Optional[float],
                               load_power: Optional[float]) -> None:
        """Servo the Force Import setpoint, pause/resume around unsafe conditions, and
        auto-stop early once the target's reached.

        Called once per poll cycle (from the end of _poll_inverter) for any
        inverter with an active Force Import tracked in self._force_import_state.

        While NOT paused, checks (in order):

        1. Pause conditions - ported from Hillview Lodge's integration_alpha_ess.yaml
           (AlphaESS_Force_Import_Pause), ANDed/ORed exactly as that source does:
           the target can't actually be met without draining the battery
           (``load - PV > target_import``, sustained via _track_sustained -
           this is the only one of the three with a hold requirement in the
           source), OR PV production is clipping against the AC limit
           (instantaneous), OR the inverter has left Normal work mode at all
           (instantaneous - covers a grid-outage Bypass/EPS event). If
           triggered, writes a neutral dispatch (word 0/Start=0, matching
           Hillview's exact register values including its own 90s duration -
           preserved as observed rather than guessed at, since it's unclear
           whether that value is even interpreted when Start=0) and marks the
           session paused rather than stopped: the overall duration/auto-off
           keeps counting down regardless, matching the source.
        2. Early stop - if batteryPower has stayed within
           +-FORCE_IMPORT_NEAR_ZERO_BAND_W for FORCE_IMPORT_NEAR_ZERO_HOLD_S
           (also via _track_sustained), the inverter's own SoC Control loop
           has already reached the cutoff and settled - stop now rather than
           waiting out the full duration.
        3. Grid-error servo - trims the charge word so measured grid power
           converges on the configured import target. Proportional control
           only, rewriting just often enough to matter.

        While paused, checks the resume gate instead: work mode confirmed
        Normal for a full FORCE_IMPORT_RESUME_NORMAL_HOLD_S (10 minutes, via
        _track_sustained) AND the overload/clipping conditions have cleared
        (both instantaneous, matching the source). Once resumed, clears the
        pause tracking and falls through into the normal checks above using
        this same poll's data, so correction resumes immediately rather than
        waiting a further cycle.

        Args:
            dev (indigo.Device): The AlphaESS Inverter device.
            grid_ok (bool): Whether this poll's grid cluster read succeeded.
            grid_power (Optional[float]): This poll's gridPower reading, if grid_ok.
            battery_ok (bool): Whether this poll's battery cluster read succeeded.
            battery_power (Optional[float]): This poll's batteryPower reading, if battery_ok.
            solar_ok (bool): Whether this poll's solar/inverter-health cluster read succeeded
                (invWorkMode lives in this cluster).
            work_mode (Optional[int]): This poll's raw invWorkMode code, if solar_ok
                (1 = Normal - see INVERTER_WORK_MODE_LABELS).
            pv_power (Optional[float]): This poll's summed PV power, if solar_ok.
            load_power (Optional[float]): This poll's computed house load, if
                grid_ok/battery_ok/solar_ok all succeeded this cycle.
        """
        state = self._force_import_state.get(dev.id)
        if state is None:
            return

        if state["paused"]:
            if solar_ok and work_mode is not None and pv_power is not None and load_power is not None:
                normal_sustained = self._track_sustained(
                    state, "normal_since", "normal_count", work_mode == 1, FORCE_IMPORT_RESUME_NORMAL_HOLD_S,
                )
                overload_cleared = (load_power - pv_power) < state["target_import_w"]
                not_clipping = pv_power < state["power_limit_w"]
                if normal_sustained and overload_cleared and not_clipping:
                    state["paused"] = False
                    state["near_zero_since"] = None
                    state["near_zero_count"] = 0
                    self.logger.info(
                        f"{dev.name}: Force Import resumed - work mode Normal, overload cleared "
                        f"(pv={pv_power:.0f}W load={load_power:.0f}W target={state['target_import_w']:.0f}W)"
                    )
                    # Fall through into the normal checks below using this same
                    # poll's data, rather than waiting a further cycle.
                else:
                    return
            else:
                # Can't evaluate the resume gate without a fresh work
                # mode/PV/load reading this cycle - stay paused, try again
                # next poll rather than resuming on stale/missing data.
                return

        if not state["paused"] and solar_ok and pv_power is not None:
            overload_now = load_power is not None and (load_power - pv_power) > state["target_import_w"]
            overload_sustained = self._track_sustained(
                state, "overload_since", "overload_count", overload_now, FORCE_IMPORT_OVERLOAD_HOLD_S,
            )
            clipping = pv_power >= state["power_limit_w"]
            not_normal = work_mode is not None and work_mode != 1
            if overload_sustained or clipping or not_normal:
                reason = (
                    "inverter not in Normal work mode" if not_normal
                    else "PV output clipping against AC limit" if clipping
                    else "target unreachable without draining the battery"
                )
                opened = self._open_dispatch_client(dev)
                if opened is not None:
                    client, unit_id = opened
                    try:
                        result = client.write_registers(
                            DISPATCH_START_ADDRESS,
                            [0, 0, 32000, 0, 32000, 0, 0, 0, 90, DISPATCH_FLOW_DIRECTION, DISPATCH_PV_UNCHANGED],
                            device_id=unit_id,
                        )
                        if result.isError():
                            raise ModbusException(f"error writing dispatch pause: {result}")
                        state["paused"] = True
                        state["last_power_raw"] = 32000
                        state["last_write_time"] = time.time()
                        state["near_zero_since"] = None
                        state["near_zero_count"] = 0
                        pv_txt = f"{pv_power:.0f}W" if pv_power is not None else "?"
                        load_txt = f"{load_power:.0f}W" if load_power is not None else "?"
                        batt_txt = f"{battery_power:.0f}W" if battery_ok and battery_power is not None else "?"
                        self.logger.warning(
                            f"{dev.name}: Force Import paused - {reason} "
                            f"(pv={pv_txt} load={load_txt} battery={batt_txt} target={state['target_import_w']:.0f}W)"
                        )
                        dev.updateStatesOnServer([{"key": "dispatchModeLabel", "value": f"Force Import - Paused ({reason})"}])
                    except ModbusException:
                        self.logger.exception(f"{dev.name}: error pausing Force Import")
                    finally:
                        client.close()
                return

        if battery_ok and battery_power is not None:
            near_zero_reached = self._track_sustained(
                state, "near_zero_since", "near_zero_count",
                abs(battery_power) <= FORCE_IMPORT_NEAR_ZERO_BAND_W, FORCE_IMPORT_NEAR_ZERO_HOLD_S,
            )
            if near_zero_reached:
                self._stop_dispatch(dev, reason="Force Import target reached")
                return

        if not grid_ok or grid_power is None:
            return

        error_w = state["target_import_w"] - float(grid_power)
        new_charge_w = (32000 - state["last_power_raw"]) + FORCE_IMPORT_SERVO_GAIN * error_w
        new_charge_w = max(0.0, min(state["power_limit_w"], new_charge_w))
        word = int(round(32000 - new_charge_w))

        now = time.time()
        changed = abs(word - state["last_power_raw"]) >= FORCE_IMPORT_SERVO_DEADBAND_W
        stale = (now - state["last_write_time"]) >= FORCE_IMPORT_STALE_REWRITE_S
        if not (changed or stale):
            return

        opened = self._open_dispatch_client(dev)
        if opened is None:
            return
        client, unit_id = opened
        try:
            self._write_dispatch(
                client, unit_id, power_raw=word, mode=DISPATCH_MODE_SOC_CONTROL,
                soc_raw=state["soc_raw"], duration_s=state["duration_s"],
            )
        except ModbusException:
            self.logger.exception(f"{dev.name}: error servoing Force Import setpoint")
            return
        finally:
            client.close()

        state["last_power_raw"] = word
        state["last_write_time"] = now
        dev.updateStatesOnServer([
            {"key": "dispatchPowerTarget", "value": -int(new_charge_w)},
            {"key": "dispatchModeLabel", "value": DISPATCH_MODE_LABELS.get(DISPATCH_MODE_SOC_CONTROL, "State of Charge Control")},
        ])
        self.logger.debug(
            f"{dev.name}: Force Import servo - grid={grid_power:.0f}W target={state['target_import_w']:.0f}W "
            f"error={error_w:.0f}W -> charge={new_charge_w:.0f}W"
        )

    def force_charging_action(self, pluginAction: indigo.PluginAction) -> None:
        """Actions.xml callback for Force Charging.

        Writes the Dispatch block with mode 2 (State of Charge Control),
        charging at the configured power until the cutoff SoC or duration
        elapses - whichever the inverter reaches first.

        Args:
            pluginAction (indigo.PluginAction): The action instance, including
                ``deviceId`` and any per-call field overrides in ``props``.
        """
        dev = indigo.devices[pluginAction.deviceId]
        power_kw = self._resolve_number(pluginAction.props.get("power"), dev.pluginProps.get("forceChargingPower"), 5.0)
        cutoff_soc = self._resolve_number(pluginAction.props.get("cutoffSoC"), dev.pluginProps.get("forceChargingCutoffSoC"), 100.0)
        duration_min = self._resolve_number(pluginAction.props.get("duration"), dev.pluginProps.get("forceChargingDuration"), 120.0)
        power_limit_kw = min(DISPATCH_POWER_ABSOLUTE_MAX_KW, self._resolve_power_limit_kw(dev))
        if not self._validate_dispatch_number(dev, "Force Charging Power (kW)", power_kw, 0.0, power_limit_kw):
            return
        if not self._validate_dispatch_number(dev, "Force Charging Cutoff SoC (%)", cutoff_soc, DISPATCH_SOC_MIN, DISPATCH_SOC_MAX):
            return
        if not self._validate_dispatch_number(dev, "Force Charging Duration (min)", duration_min, DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES):
            return
        duration_s = int(duration_min * 60)
        if duration_s <= 0:
            self.logger.error(f"{dev.name}: Force Charging duration must be greater than 0")
            return
        power_raw = int(32000 - power_kw * 1000)
        soc_raw = int(cutoff_soc / DISPATCH_SOC_SCALE)
        self._start_dispatch(
            dev, dispatch_type="forceCharging", power_raw=power_raw, mode=DISPATCH_MODE_SOC_CONTROL,
            soc_raw=soc_raw, duration_s=duration_s, dispatch_power_w=int(-power_kw * 1000),
        )

    def force_discharging_action(self, pluginAction: indigo.PluginAction) -> None:
        """Actions.xml callback for Force Discharging.

        Writes the Dispatch block with mode 2 (State of Charge Control),
        discharging at the configured power until the cutoff SoC or duration
        elapses - whichever the inverter reaches first.

        Args:
            pluginAction (indigo.PluginAction): The action instance, including
                ``deviceId`` and any per-call field overrides in ``props``.
        """
        dev = indigo.devices[pluginAction.deviceId]
        power_kw = self._resolve_number(pluginAction.props.get("power"), dev.pluginProps.get("forceDischargingPower"), 5.0)
        cutoff_soc = self._resolve_number(pluginAction.props.get("cutoffSoC"), dev.pluginProps.get("forceDischargingCutoffSoC"), 20.0)
        duration_min = self._resolve_number(pluginAction.props.get("duration"), dev.pluginProps.get("forceDischargingDuration"), 120.0)
        power_limit_kw = min(DISPATCH_POWER_ABSOLUTE_MAX_KW, self._resolve_power_limit_kw(dev))
        if not self._validate_dispatch_number(dev, "Force Discharging Power (kW)", power_kw, 0.0, power_limit_kw):
            return
        if not self._validate_dispatch_number(dev, "Force Discharging Cutoff SoC (%)", cutoff_soc, DISPATCH_SOC_MIN, DISPATCH_SOC_MAX):
            return
        if not self._validate_dispatch_number(dev, "Force Discharging Duration (min)", duration_min, DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES):
            return
        duration_s = int(duration_min * 60)
        if duration_s <= 0:
            self.logger.error(f"{dev.name}: Force Discharging duration must be greater than 0")
            return
        power_raw = int(32000 + power_kw * 1000)
        soc_raw = int(cutoff_soc / DISPATCH_SOC_SCALE)
        self._start_dispatch(
            dev, dispatch_type="forceDischarging", power_raw=power_raw, mode=DISPATCH_MODE_SOC_CONTROL,
            soc_raw=soc_raw, duration_s=duration_s, dispatch_power_w=int(power_kw * 1000),
        )

    def force_import_action(self, pluginAction: indigo.PluginAction) -> None:
        """Actions.xml callback for Force Import.

        Unlike Force Charging/Discharging/generic Dispatch (a single
        fire-and-forget write), Force Import needs a standing servo loop: the
        dispatch block only accepts a fixed charge-power word, but the actual
        goal is a fixed *grid import* level, and house load/PV shift
        constantly. This writes an initial feed-forward estimate -
        ``charge = target_import - house_load + pv`` (floored at 0), matching
        senalse/ha-alphaess-modbus's switch.py's ``_compute_force_import_power_raw``
        exactly - then hands off to ``_service_force_import`` (called once per
        poll from ``_poll_inverter``) to correct the setpoint against the
        actual measured grid power and to stop early once the battery settles
        (the inverter's own SoC Control loop has reached the cutoff).

        Args:
            pluginAction (indigo.PluginAction): The action instance, including
                ``deviceId`` and any per-call field overrides in ``props``.
        """
        dev = indigo.devices[pluginAction.deviceId]
        import_power_kw = self._resolve_number(pluginAction.props.get("power"), dev.pluginProps.get("forceImportPower"), 5.0)
        cutoff_soc = self._resolve_number(pluginAction.props.get("cutoffSoC"), dev.pluginProps.get("forceImportCutoffSoC"), 90.0)
        duration_min = self._resolve_number(pluginAction.props.get("duration"), dev.pluginProps.get("forceImportDuration"), 120.0)
        power_limit_kw = min(DISPATCH_POWER_ABSOLUTE_MAX_KW, self._resolve_power_limit_kw(dev))
        if not self._validate_dispatch_number(dev, "Force Import Power (kW)", import_power_kw, 0.0, power_limit_kw):
            return
        if not self._validate_dispatch_number(dev, "Force Import Cutoff SoC (%)", cutoff_soc, DISPATCH_SOC_MIN, DISPATCH_SOC_MAX):
            return
        if not self._validate_dispatch_number(dev, "Force Import Duration (min)", duration_min, DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES):
            return
        duration_s = int(duration_min * 60)
        if duration_s <= 0:
            self.logger.error(f"{dev.name}: Force Import duration must be greater than 0")
            return

        target_import_w = import_power_kw * 1000
        power_limit_w = power_limit_kw * 1000
        soc_raw = int(cutoff_soc / DISPATCH_SOC_SCALE)

        # Feed-forward seed: accurate at turn-on, before the battery is
        # dispatching - the servo takes over from the next poll. Falls back to
        # "assume charge == target" if there's no cached reading yet (e.g. a
        # brand-new device); the servo corrects it from the next poll either way.
        solar_dev = self._find_child(dev.id, "solarDevice")
        pv_power = solar_dev.states.get("pvPower") if solar_dev else None
        load_power = dev.states.get("loadPower")
        if pv_power is not None and load_power is not None:
            charge_w = max(0.0, target_import_w - float(load_power) + float(pv_power))
        else:
            charge_w = target_import_w
        power_raw = int(32000 - charge_w)

        started = self._start_dispatch(
            dev, dispatch_type="forceImport", power_raw=power_raw, mode=DISPATCH_MODE_SOC_CONTROL,
            soc_raw=soc_raw, duration_s=duration_s, dispatch_power_w=int(-charge_w),
        )
        if not started:
            return
        self._force_import_state[dev.id] = {
            "target_import_w": target_import_w,
            "soc_raw": soc_raw,
            "duration_s": duration_s,
            "power_limit_w": power_limit_w,
            "last_power_raw": power_raw,
            "last_write_time": time.time(),
            "near_zero_since": None,
            "near_zero_count": 0,
            "paused": False,
            "overload_since": None,
            "overload_count": 0,
            "normal_since": None,
            "normal_count": 0,
        }

    def dispatch_action(self, pluginAction: indigo.PluginAction) -> None:
        """Actions.xml callback for the generic Dispatch action (all 8 modes).

        Unlike Force Charging/Discharging, the power word only applies in
        modes 1/2/3/5 - modes 4/6/7/19 are algorithm-driven and always run
        neutral regardless of the power field - and the SoC-target word only
        applies in mode 2. See DISPATCH_MODE_LABELS and the module-level
        comment above DISPATCH_START_ADDRESS.

        Args:
            pluginAction (indigo.PluginAction): The action instance, including
                ``deviceId`` and its ``mode``/``power``/``cutoffSoC``/``duration``/
                ``pvSwitch`` fields.
        """
        dev = indigo.devices[pluginAction.deviceId]
        try:
            mode = int(pluginAction.props.get("mode", DISPATCH_MODE_SOC_CONTROL))
        except (TypeError, ValueError):
            mode = DISPATCH_MODE_SOC_CONTROL
        power_kw = self._resolve_number(pluginAction.props.get("power"), None, 0.0)
        cutoff_soc = self._resolve_number(pluginAction.props.get("cutoffSoC"), None, 100.0)
        duration_min = self._resolve_number(pluginAction.props.get("duration"), None, 120.0)
        if not self._validate_dispatch_number(dev, "Dispatch Duration (min)", duration_min, DISPATCH_DURATION_MIN_MINUTES, DISPATCH_DURATION_MAX_MINUTES):
            return
        duration_s = int(duration_min * 60)
        if duration_s <= 0:
            self.logger.error(f"{dev.name}: Dispatch duration must be greater than 0")
            return
        try:
            pv_switch = int(pluginAction.props.get("pvSwitch", DISPATCH_PV_UNCHANGED))
        except (TypeError, ValueError):
            pv_switch = DISPATCH_PV_UNCHANGED

        if mode in (1, 2, 3, 5):
            # Power only applies in these modes (see the module-level DISPATCH_MODE_LABELS
            # comment) - only validated here, not for the algorithm-driven modes below,
            # where a leftover/default power value is never actually sent.
            power_limit_kw = min(DISPATCH_POWER_ABSOLUTE_MAX_KW, self._resolve_power_limit_kw(dev))
            if not self._validate_dispatch_number(dev, "Dispatch Power (kW)", power_kw, -power_limit_kw, power_limit_kw):
                return
            power_raw = int(32000 + power_kw * 1000)
            dispatch_power_w = int(power_kw * 1000)
        else:
            power_raw = 32000
            dispatch_power_w = 0
        if mode == DISPATCH_MODE_SOC_CONTROL:
            # SoC target only applies in this mode - same reasoning as Power above.
            if not self._validate_dispatch_number(dev, "Dispatch Cutoff SoC (%)", cutoff_soc, DISPATCH_SOC_MIN, DISPATCH_SOC_MAX):
                return
            soc_raw = int(cutoff_soc / DISPATCH_SOC_SCALE)
        else:
            soc_raw = 0

        self._start_dispatch(
            dev, dispatch_type="dispatch", power_raw=power_raw, mode=mode, soc_raw=soc_raw,
            duration_s=duration_s, dispatch_power_w=dispatch_power_w, pv_switch=pv_switch,
        )

    def dispatch_reset_action(self, pluginAction: indigo.PluginAction) -> None:
        """Actions.xml callback for Dispatch Reset - stops any active dispatch.

        Args:
            pluginAction (indigo.PluginAction): The action instance, including ``deviceId``.
        """
        dev = indigo.devices[pluginAction.deviceId]
        self._stop_dispatch(dev, reason="user requested")

    def _resolve_single_inverter(self, menu_label: str) -> Optional[indigo.Device]:
        """Resolve the one AlphaESS Inverter device for a bare Plugins-menu item.

        Shared by every menu item that acts on a device with no picker of its
        own (unlike an Action Group step, which always specifies a device).
        Only succeeds when exactly one Inverter device exists - with zero or
        multiple, there's no context to disambiguate from a bare menu click,
        so this logs an error and returns None rather than guessing which
        device was meant. A multi-inverter install always has the equivalent
        Action-Group version of each of these, with its own device picker.

        Args:
            menu_label (str): The menu item's own name, used in the error message.

        Returns:
            Optional[indigo.Device]: The single Inverter device, or None if
                zero or more than one exists (already logged).
        """
        inverters = list(indigo.devices.iter("self.inverter"))
        if len(inverters) != 1:
            self.logger.error(
                f"{menu_label} menu item needs exactly one AlphaESS Inverter device configured "
                f"(found {len(inverters)}) - use the equivalent Action in an Action Group instead, "
                f"which lets you pick the device."
            )
            return None
        return inverters[0]

    def dispatch_reset_menu_action(self) -> None:
        """MenuItems.xml callback for the Plugins-menu "Dispatch Reset (Stop)" item.

        Deliberately has no ConfigUI/device picker, unlike dispatch_reset_action -
        the point is a one-click stop for the common single-inverter install
        without a dialog in the way.
        """
        dev = self._resolve_single_inverter("Dispatch Reset (Stop)")
        if dev is None:
            return
        self._stop_dispatch(dev, reason="user requested (menu)")

    def poll_now_menu_action(self) -> None:
        """MenuItems.xml callback for the Plugins-menu "Poll Now" item.

        Reads a fresh set of registers immediately, without waiting for the
        device's own configured pollInterval to elapse. Purely additive -
        _next_poll_at (runConcurrentThread's own schedule) is only ever
        touched inside that loop, never inside _poll_inverter itself, so
        calling it directly here doesn't disturb or reset the normal polling
        cadence in any way; it's just one extra read layered on top.
        """
        dev = self._resolve_single_inverter("Poll Now")
        if dev is None:
            return
        self.logger.info(f"{dev.name}: Polling now (menu)")
        self._poll_inverter(dev)

    def force_charging_menu_action(self) -> None:
        """MenuItems.xml callback for the Plugins-menu "Force Charging" item.

        One-click Force Charging using the Inverter device's own configured
        defaults (forceChargingPower/CutoffSoC/Duration) - deliberately no
        dialog, same "quick action" posture as the other menu items here.
        Delegates to force_charging_action itself (rather than duplicating
        its validation/resolution logic) via a minimal stand-in object that
        supplies just the two attributes that callback reads - deviceId and
        an empty props dict, which makes every one of its own overridable
        fields resolve straight through to the device defaults exactly as
        if a real Action Group step had left them all blank.
        """
        dev = self._resolve_single_inverter("Force Charging")
        if dev is None:
            return
        self.force_charging_action(_MenuPluginAction(dev.id))

    def force_discharging_menu_action(self) -> None:
        """MenuItems.xml callback for the Plugins-menu "Force Discharging" item.

        Same one-click, defaults-only pattern as force_charging_menu_action -
        see that method's docstring.
        """
        dev = self._resolve_single_inverter("Force Discharging")
        if dev is None:
            return
        self.force_discharging_action(_MenuPluginAction(dev.id))

    def toggle_debug_logging_menu_action(self) -> None:
        """MenuItems.xml callback for the Plugins-menu "Toggle Debug Logging" item.

        Flips showDebugInfo the same way closedPrefsConfigUi does when the
        Configure dialog's checkbox is saved - just without the dialog.
        Writing directly to self.pluginPrefs is the same mechanism the
        Configure dialog itself uses to persist a value; no separate save
        call is needed, Indigo persists this dict automatically.
        """
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'} (via menu)")

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
