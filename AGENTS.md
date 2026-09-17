# AGENTS.md

## Project overview

nut2mqtt is a single-file Python bridge (`nut2mqtt.py`) that polls UPS data
from Network UPS Tools (NUT) via `upsc`/`upscmd` and publishes it to MQTT
using Home Assistant MQTT Discovery. Runs as a systemd service or a
container.

## Setup

```
pip install -r requirements.txt
```

## Build / Run

```
python nut2mqtt.py
```

Reads config from `config.yaml` (see `config.yaml.example`). A container
build is available via the `Dockerfile`; `docker/entrypoint.sh` is its
entrypoint script.

## Test

```
pip install pytest
pytest
```

Tests live in `test_nut2mqtt.py` at the repo root and mock the NUT/MQTT
calls — no live NUT server or broker required.

## Repository structure

- `nut2mqtt.py` — the bridge itself (single module)
- `test_nut2mqtt.py` — unit tests
- `config.yaml.example` — example runtime config
- `docker/` — container entrypoint script
- `nut/` — example NUT server config files for local testing
- `systemd/` — systemd unit file for running as a service

## Commit and PR conventions

- Commit messages and PR titles must follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`, `build:`, `perf:`, `style:`, `revert:`), optionally with a scope, e.g. `fix(api): handle null response`.
- This repo squash-merges pull requests only; the PR title becomes the final commit message on `main`.
- A "Conventional Commits" CI check enforces this on both PR titles and direct-push commit messages.
- Branch protection on `main`: no force-pushes, no branch deletion, required status checks must pass.
