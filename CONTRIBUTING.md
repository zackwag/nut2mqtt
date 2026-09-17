# Contributing to nut2mqtt

nut2mqtt is a lightweight Python bridge that polls UPS data from Network UPS
Tools (NUT) via `upsc`/`upscmd` and publishes it to MQTT for Home Assistant
discovery. Contributions — bug fixes, new NUT variable mappings, packaging
improvements — are welcome.

## Getting started

```
git clone https://github.com/zackwag/nut2mqtt.git
cd nut2mqtt
pip install -r requirements.txt
```

You'll also need a running NUT setup (or the example configs in `nut/`) and
an MQTT broker to exercise the bridge end-to-end; the unit tests do not
require either.

## Development

Run the bridge locally against `config.yaml` (copy `config.yaml.example` as a
starting point):

```
python nut2mqtt.py
```

Run the test suite:

```
pip install pytest
pytest
```

## Commit messages and pull requests

This repo uses [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, etc.). Pull requests are squash-merged, and the **PR title** becomes the commit on `main` — so PR titles must follow this format. This is enforced automatically by the "Conventional Commits" check.

Direct pushes to `main` are allowed but must also use a Conventional Commits-formatted commit message (validated by the same check).

## Opening a pull request

1. Fork the repo and create a branch off `main`.
2. Make your changes.
3. Open a pull request with a Conventional Commits-formatted title.
4. Wait for CI to pass — required checks must be green before merge.

## Reporting issues

Use [GitHub Issues](../../issues) for bugs and feature requests.
