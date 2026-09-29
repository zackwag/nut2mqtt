# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Commands

```
pytest                                                    # full suite
pytest test_nut2mqtt.py::TestReadUps                      # one class
pytest test_nut2mqtt.py::TestReadUps::test_parses_output  # one test
ruff format --check . && ruff check .                     # what the Lint CI job runs
ruff format .                                             # auto-format (line-length 100, see pyproject.toml)
```

Tests import functions directly from `nut2mqtt` and mock `subprocess.run` / the MQTT client, so keep new logic in small importable functions rather than inline in `main()`.

## Architecture

Everything lives in `nut2mqtt.py`. `main()` wires it up in this order: load config → connect MQTT (with LWT) → build HA device info → publish retained discovery configs → subscribe to command topics → enter `poll_loop()`.

**Entity types and their lookup tables.** Each configured entity type has a `build_*_discovery()` (pure payload builder, returns `(payload, entity_id)` or `None`) and a `publish_*` function that publishes discovery and returns a lookup dict consumed later:
- `sensors` → `sensor_lookup` `{entity_id: {key, state_topic, beeper}}`, read by the poll loop.
- `commands` (HA buttons) → `command_lookup` `{command_topic: nut_command}`, read by `on_message`.
- `switches` → two lookups: `switch_command_lookup` keyed by command topic (for `on_message`) and `switch_state_lookup` keyed by entity_id (for the poll loop).
- The "Connected" binary sensor is always published and has no lookup.

**Two availability topics.** `<base>/<slug>_sensors/availability` is the MQTT LWT topic and gates every sensor/button/switch. `<base>/<slug>_connected/availability` is the state topic of the Connected binary sensor. Both are set `online`/`offline` together by `process_poll()` depending on whether `upsc` returned data, and both are set `offline` on SIGINT/SIGTERM.

**State publishing.** `process_poll()` only publishes a value when it differs from `last_values`, which is persisted to `last_values.json` so a restart doesn't republish everything; entries missing from the UPS data are pruned. After a button/switch command succeeds, `on_message` sleeps `settle_seconds` and calls `process_poll()` immediately — note this runs on paho's network thread (`loop_start()`), sharing `sensor_lookup`/`last_values` with the main poll loop.

**SIGHUP reload** re-reads only `sensors`. `make_reload_handler()` mutates `sensor_lookup` in place (`clear()` + `update()`) because the poll loop and `on_message` hold references to that same dict — don't reassign it. `mqtt`, `ups`, `commands`, and `switches` changes need a restart.

**Device identity.** `setup_device_info()` reads manufacturer/model/firmware once at startup and falls back to `device_info.json` for fields the driver hasn't reported yet (common right after a restart). `docker/entrypoint.sh` waits for a real polled value (`ups.status`) before starting the bridge for the same reason.

**Naming.** HA device `identifiers` come from `sanitize_slug(ups.name)`; entity IDs and availability topics come from `ups.friendly_name` via `make_entity_id()`, which strips a duplicated device-name prefix. Changing either renames entities in Home Assistant. Discovery topics use the hard-coded `homeassistant/` prefix.

**Paths.** `config.yaml`, `last_values.json`, and `device_info.json` are relative to the working directory (`/opt/nut2mqtt` under systemd, `/data` in the container).

## Conventions

- Broad `except Exception` is used deliberately where the daemon must survive (poll loop, subprocess calls, persistence), always with a `# noqa: BLE001 - <reason>` comment. Follow that pattern rather than letting exceptions escape the loop.
- New config keys: read them in `main()` with a `DEFAULT_*` module constant, add required ones to `validate_config()`, and document them in the README config reference tables and `config.yaml.example`.
- Releases are driven by release-please: it bumps `__version__` in `nut2mqtt.py` (the `x-release-please-version` marker), `pyproject.toml`, and `CHANGELOG.md`. Don't edit those by hand. Merging the release PR pushes a `v*` tag, which triggers the multi-arch Docker Hub publish.
