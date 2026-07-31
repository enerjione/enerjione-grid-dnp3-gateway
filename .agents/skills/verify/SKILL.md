---
name: verify
description: Run DNP3 gateway against a local mock backend.
---

1. Launch a local HTTP mock serving config, `commands/pending`, telemetry ingest, and command-results endpoints.
2. Run `.venv\Scripts\python.exe -m dnp3_gateway` with `PYTHONPATH=src`, `GATEWAY_MODE=mock`, a unique `GATEWAY_STATE_DIR`, and unique health/backend ports.
3. Query gateway `/health` and mock backend state. Verify repeated pending-command responses produce one `command-results` POST per ID.
4. Stop both processes and remove mock scripts, logs, and state artifacts.
