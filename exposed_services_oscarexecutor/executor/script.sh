#!/bin/bash
################################################################################
# script.sh — OSCAR FaaS Supervisor user script for airflow-worker service
#
# Supports two invocation modes:
#
# 1. WORKLOAD MODE (production — used by OSCARExecutor):
#    {
#      "mode": "workload",
#      "workload_json": "<serialized ExecuteTask from workload.model_dump_json()>"
#    }
#    Runs: python -m airflow.sdk.execution_time.execute_workload --json-string <workload_json>
#
# 2. COMMAND MODE (testing/debugging):
#    {
#      "command": ["airflow", "tasks", "test", ...],
#      "task_key": {}
#    }
#    Runs the command directly.
################################################################################

set -eu

python3 << 'PYTHON_EOF'
import json
import os
import subprocess
import sys

import boto3
from botocore.client import Config as BotoConfig

input_file = os.environ["INPUT_FILE_PATH"]
output_dir = os.environ["TMP_OUTPUT_DIR"]

# ── MinIO config from env ────────────────────────────────────────────────
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "airflow")
MINIO_DAGS_PREFIX = os.environ.get("MINIO_DAGS_PREFIX", "dags/")
DAGS_LOCAL_DIR = os.environ.get("AIRFLOW__CORE__DAGS_FOLDER", "/tmp/dags/")


def sync_dags():
    """Download all DAG files from MinIO to the local dags directory."""
    if not MINIO_ENDPOINT:
        print("[oscar-airflow-worker] WARNING: MINIO_ENDPOINT not set, skipping DAG sync")
        return False

    print(f"[oscar-airflow-worker] Syncing DAGs from {MINIO_ENDPOINT}/{MINIO_BUCKET}/{MINIO_DAGS_PREFIX}")

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            config=BotoConfig(signature_version="s3v4"),
            region_name="us-east-1",
        )

        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=MINIO_BUCKET, Prefix=MINIO_DAGS_PREFIX)

        os.makedirs(DAGS_LOCAL_DIR, exist_ok=True)
        count = 0

        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                relative = key[len(MINIO_DAGS_PREFIX):] if key.startswith(MINIO_DAGS_PREFIX) else key
                if not relative:
                    continue
                local_path = os.path.join(DAGS_LOCAL_DIR, relative)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                s3.download_file(MINIO_BUCKET, key, local_path)
                print(f"[oscar-airflow-worker] Downloaded: {key} -> {local_path}")
                count += 1

        print(f"[oscar-airflow-worker] DAG sync complete: {count} files downloaded")
        return count > 0

    except Exception as e:
        print(f"[oscar-airflow-worker] ERROR syncing DAGs: {e}", file=sys.stderr)
        return False


# ── 1. Sync DAGs from MinIO ─────────────────────────────────────────────
sync_dags()

# ── 2. Read the payload ─────────────────────────────────────────────────
with open(input_file, "r") as f:
    payload = json.load(f)

mode = payload.get("mode", "command")

if mode == "workload":
    # ── WORKLOAD MODE: Task SDK execution (production path) ──────────
    workload_json = payload.get("workload_json")
    if not workload_json:
        error = {"status": "failed", "exit_code": 1, "error": "Missing 'workload_json' in payload"}
        with open(os.path.join(output_dir, "result.json"), "w") as f:
            json.dump(error, f)
        sys.exit(1)

    # Write the workload to a temp file for --json-path
    # (avoids shell argument length limits for large workloads)
    workload_path = "/tmp/workload_input.json"
    with open(workload_path, "w") as f:
        f.write(workload_json)

    command = [
        "python", "-m", "airflow.sdk.execution_time.execute_workload",
        "--json-path", workload_path,
    ]

    print(f"[oscar-airflow-worker] WORKLOAD MODE: executing via Task SDK")

else:
    # ── COMMAND MODE: direct CLI execution (testing/debugging) ───────
    command = payload.get("command")
    if not command or not isinstance(command, list):
        error = {"status": "failed", "exit_code": 1, "error": "Missing or invalid 'command' field"}
        with open(os.path.join(output_dir, "result.json"), "w") as f:
            json.dump(error, f)
        sys.exit(1)

    command = [str(c) for c in command]
    print(f"[oscar-airflow-worker] COMMAND MODE: {' '.join(command)}")

# ── 3. Execute and capture output ────────────────────────────────────────
result = subprocess.run(command, capture_output=True, text=True)
exit_code = result.returncode

status = "success" if exit_code == 0 else "failed"
print(f"[oscar-airflow-worker] status={status}, exit_code={exit_code}")
print(f"[oscar-airflow-worker] STDOUT:\n{result.stdout}")
if result.stderr:
    print(f"[oscar-airflow-worker] STDERR:\n{result.stderr}", file=sys.stderr)

# ── 4. Write output ─────────────────────────────────────────────────────
task_key = payload.get("task_key", {})
output = {
    "status": status,
    "exit_code": exit_code,
    "task_key": task_key,
    "stdout": result.stdout[-2000:] if result.stdout else "",
    "stderr": result.stderr[-2000:] if result.stderr else "",
}

with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(output, f)

sys.exit(exit_code)
PYTHON_EOF