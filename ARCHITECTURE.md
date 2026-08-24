# Architecture notes

Internal notes on how `plugin.py` is put together, for future maintenance. See `README.md` for user-facing documentation.

## Polling model

Indigo's `runConcurrentThread` runs a plain synchronous loop, ticking every 5 seconds. Each enabled `inverter` device is polled independently once its own configured `pollInterval` has elapsed (tracked in `self._next_poll_at`), so multiple inverters can be polled at different rates without blocking each other — a failure on one device (caught individually, logged via `logger.exception`) never stops the loop or affects other devices.

## Register reads (`_poll_inverter`)

Each poll opens a fresh `ModbusTcpClient` connection, does 3 batched `read_holding_registers` calls (`REGISTER_CLUSTERS`) covering every address `REGISTERS` needs plus the 6-string PV block, and closes the connection in a `finally` block. Batching exists purely to keep each poll to 3 round trips instead of 12+.

`_decode_value` combines 1 or 2 raw 16-bit registers into a signed/scaled float — `words: 2` means high-word-first, two's-complement if `signed`. `pvPower` is *not* read from the single "PV meter" register (161) — that's an optional external CT accessory that reads flat 0 without one wired up. It's computed instead by summing the inverter's own 6 MPPT string power registers (`PV_STRING_POWER_ADDRESSES`), which read directly off the trackers.

## Error states

`dev.setErrorStateOnServer(...)` is set for every known failure path — missing/non-numeric address, port, or unit ID; TCP connect failure; a Modbus exception response; and (as a catch-all) any other unexpected exception during decode — and cleared (`None`) only after a full poll succeeds and states are written. A single Modbus exception on *any* register cluster currently fails the whole poll (nothing partial is written) — see the note in `README.md`'s battery/solar section if that ever needs to become more granular for battery-less installations.

## Single-connection limitation

AlphaESS inverters accept exactly one Modbus TCP connection at a time — a second concurrent connection gets an immediate refused connection, not a timeout. This isn't handled specially in code; it just surfaces as an ordinary "Connection failed" error state if something else (Home Assistant, etc.) is already connected. See `README.md`'s troubleshooting section for the TCP-proxy workaround.

## HTTP dashboard (`dashboard` / `dashboard_data`)

Reachable at `http://<host>:8176/message/com.coolcaper.alphaessmodbus/<id>` via Indigo's built-in plugin HTTP responder — registered in `Actions.xml` as `<Action id="..." uiPath="hidden"><CallbackMethod>...</CallbackMethod></Action>`, per Indigo's official `Example HTTP Responder` SDK sample. **Both callback signatures must stay untyped** (`def dashboard(self, action, dev=None, caller_waiting_for_result=None):`, no type hints or return annotation) — Indigo's HTTP-dispatch bridge fails on this specific path with an opaque `RuntimeError: unable to convert python exception`, raised *before* the method body ever runs, if the signature carries type hints. Both methods also wrap their own bodies in `try/except Exception` and log via `self.logger.exception` before returning a `500` — Indigo's own exception-marshalling for this dispatch path can itself fail silently, so this is the only reliable way to see what actually went wrong.

`dashboard` returns the static `DASHBOARD_HTML` page (inline CSS/JS, no CDN dependency, dark-mode aware via `prefers-color-scheme`). `dashboard_data` returns current state as JSON for the page's 5-second client-side poll; if more than one inverter device exists, it supports a `?deviceId=` query param and returns a `devices` list so the page can render a picker. All dynamic values are written into the page via `.textContent`/`createElement`, never `.innerHTML`, so a device renamed to contain HTML/script content can't inject into the dashboard.

## Dependency management

`pymodbus` is declared in `requirements.txt` and installed automatically by Indigo on first launch — no vendored copy, no `sys.path` manipulation (same mechanism [Automate Pulse 2](https://github.com/coolcaper777/automate-pulse-2) uses for `aiopulse2`). An earlier version of this plugin manually vendored `pymodbus` into `Contents/Packages`, which required a `try/except NameError` fallback for `__file__` (Indigo `exec`s `plugin.py` rather than importing it, so `__file__` isn't defined at module scope) — that workaround is gone now that Indigo's own dependency installer manages the import path.

## Debug logging

`self.debug` / `self.indigo_log_handler` level is set from the `showDebugInfo` plugin preference (`PluginConfig.xml`) in `__init__` and re-applied live in `closedPrefsConfigUi` — no restart needed to toggle it. Debug-level logging covers every decoded register value per poll, keyed by state name.
