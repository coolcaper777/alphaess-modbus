# Architecture notes

Internal notes on how `plugin.py` is put together, for future maintenance. See `README.md` for user-facing documentation.

## Polling model

Indigo's `runConcurrentThread` runs a plain synchronous loop, ticking every 5 seconds. Each enabled `inverter` device is polled independently once its own configured `pollInterval` has elapsed (tracked in `self._next_poll_at`), so multiple inverters can be polled at different rates without blocking each other — a failure on one device (caught individually, logged via `logger.exception`) never stops the loop or affects other devices.

## Device model: one connection, four devices

The plugin models the physical inverter as a parent `inverter` device plus three auto-discovered children (`solarDevice`/`batteryDevice`/`gridDevice`), each holding a `systemDevice` prop pointing back at the parent's device ID (same parent/child pattern [MyAir](https://github.com/coolcaper777/myair) uses for its zone devices). This exists because the natural grouping of AlphaESS telemetry (solar/battery/grid) maps better onto separate Indigo devices than one monolithic device — cleaner trigger/Control Page picking, and each domain can independently show its own error state rather than one shared "something's wrong" indicator.

Since AlphaESS inverters accept only **one** Modbus TCP connection at a time (see below), all four devices are updated from a *single* poll/connection per cycle — `_poll_inverter` (still keyed off the parent `inverter` device in `runConcurrentThread`/`self._next_poll_at`) does the one Modbus session and fans the results out to whichever children exist, rather than each device polling independently.

`_get_or_create_child`/`_find_child` look children up by `systemDevice == str(parent_dev.id)`; the former creates one if missing (deferred, like MyAir's zone discovery, while the parent still has Indigo's placeholder name - `NEW_DEVICE_NAME_RE`). `_set_children_error` sets an error state on whichever children already exist, without creating new ones, for failures that happen before any register read is even attempted (bad config, connect failure).

## Register reads (`_poll_inverter`)

Each poll opens a fresh `ModbusTcpClient` connection and does 3 batched `read_holding_registers` calls (`REGISTER_CLUSTERS`) - one per child device's domain (grid: 16-34, solar+inverter-health: 1053-1077, battery: 256-294) - closing the connection in a `finally` block. Batching exists purely to keep each poll to 3 round trips instead of 20+; every address in `REGISTERS` falls inside one of the three cluster ranges, so this covers the full 37-value register set the same way it covered the original 6.

`_decode_value` combines 1 or 2 raw 16-bit registers into a signed/scaled float — `words: 2` means high-word-first, two's-complement if `signed`. `pvPower` is *not* read from the single "PV meter" register (161) — that's an optional external CT accessory that reads flat 0 without one wired up. It's computed instead by summing the six individual `pv{n}Power` values (each itself read directly off an MPPT tracker, same as the per-string voltage/current now also exposed alongside it).

## Error states: per-cluster isolation

Each of the 3 register clusters is read (and its `ModbusException`, if any, caught) **independently** inside `_poll_inverter`'s loop, tracked in `cluster_registers`/`cluster_errors` keyed by cluster start address. This matters specifically because of the device split above: a failure reading the battery cluster (e.g. a battery-less installation refusing that address range - still unverified, see `README.md`) sets an error state on just the Battery device, while Solar and Grid update normally from their own successful reads. The parent Inverter device's `loadPower` is only computed (and only written) when all three clusters succeed in the same cycle - skipped rather than computed from a partial/stale mix otherwise, with `dev.setErrorStateOnServer(...)` naming which domain(s) were missing that cycle. `invTemperature` only depends on the solar cluster, so it updates independently of `loadPower`'s three-way requirement.

A genuinely unmodeled exception (decode bug, etc.) still aborts the whole poll and sets an error state on the parent plus every already-existing child (`_set_children_error`) - only the two *expected* failure modes (per-cluster Modbus errors, and total connect failure) get the more granular treatment.

## Single-connection limitation

AlphaESS inverters accept exactly one Modbus TCP connection at a time — a second concurrent connection gets an immediate refused connection, not a timeout. This isn't handled specially in code; it just surfaces as an ordinary "Connection failed" error state if something else (Home Assistant, etc.) is already connected. See `README.md`'s troubleshooting section for the TCP-proxy workaround.

## HTTP dashboard (`dashboard` / `dashboard_data`)

Reachable at `http://<host>:8176/message/com.coolcaper.alphaessmodbus/<id>` via Indigo's built-in plugin HTTP responder — registered in `Actions.xml` as `<Action id="..." uiPath="hidden"><CallbackMethod>...</CallbackMethod></Action>`, per Indigo's official `Example HTTP Responder` SDK sample. **Both callback signatures must stay untyped** (`def dashboard(self, action, dev=None, caller_waiting_for_result=None):`, no type hints or return annotation) — Indigo's HTTP-dispatch bridge fails on this specific path with an opaque `RuntimeError: unable to convert python exception`, raised *before* the method body ever runs, if the signature carries type hints. Both methods also wrap their own bodies in `try/except Exception` and log via `self.logger.exception` before returning a `500` — Indigo's own exception-marshalling for this dispatch path can itself fail silently, so this is the only reliable way to see what actually went wrong.

`dashboard` returns the static `DASHBOARD_HTML` page (inline CSS/JS, no CDN dependency, dark-mode aware via `prefers-color-scheme`). `dashboard_data` returns current state as JSON for the page's 5-second client-side poll; if more than one inverter device exists, it supports a `?deviceId=` query param and returns a `devices` list so the page can render a picker. The page's own JS and its JSON key names (`pvPower`/`gridPower`/etc.) are untouched by the Solar/Battery/Grid device split - only `dashboard_data`'s Python side changed, gathering those same keys from the three child devices via `_find_child` instead of reading them straight off one `inverter` device. All dynamic values are written into the page via `.textContent`/`createElement`, never `.innerHTML`, so a device renamed to contain HTML/script content can't inject into the dashboard.

## Dependency management

`pymodbus` is declared in `requirements.txt` and installed automatically by Indigo on first launch — no vendored copy, no `sys.path` manipulation (same mechanism [Automate Pulse 2](https://github.com/coolcaper777/automate-pulse-2) uses for `aiopulse2`). An earlier version of this plugin manually vendored `pymodbus` into `Contents/Packages`, which required a `try/except NameError` fallback for `__file__` (Indigo `exec`s `plugin.py` rather than importing it, so `__file__` isn't defined at module scope) — that workaround is gone now that Indigo's own dependency installer manages the import path.

## Debug logging

`self.debug` / `self.indigo_log_handler` level is set from the `showDebugInfo` plugin preference (`PluginConfig.xml`) in `__init__` and re-applied live in `closedPrefsConfigUi` — no restart needed to toggle it. Debug-level logging covers every decoded register value per poll, keyed by state name.
