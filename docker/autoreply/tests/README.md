# Autoreply tests

Network-free unit tests for `docker/autoreply/app.py` plus a few contract
tests on `bin/provision-zammad.sh` and `docker-compose.yml` (security
readiness Q4-2026, findings ZAM-1 … ZAM-7).

Run from the repository root with the image's Python dependencies
(`requests`) plus `pytest`; nothing else is needed:

```sh
python3 -m venv .venv && .venv/bin/pip install pytest -r docker/autoreply/requirements.txt
.venv/bin/pytest docker/autoreply -q
```

`conftest.py` points the service at throw-away secret files and dummy URLs
before `app.py` is imported, so the module-level `SERVICE = AutoreplyService()`
constructs without the live stack. The compose contract test shells out to
`docker compose config` (read-only, project name `ci-zammad-pentest`) and is
skipped when Docker is not on the PATH.
