from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.redis_queue import QueueError, TaskNotFound, public_task_payload, queue_from_env


app = Flask(__name__)


def task_queue():
    return queue_from_env()


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "api-server"})


@app.post("/generate")
def generate():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object", "code": "invalid_request"}), 400
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required", "code": "invalid_generation_request"}), 400
    try:
        task = task_queue().enqueue({"prompt": prompt})
    except QueueError as exc:
        return jsonify({"error": str(exc), "code": "queue_unavailable"}), 503
    return jsonify({"task_id": task["task_id"]}), 202


@app.get("/status/<task_id>")
def status(task_id: str):
    try:
        task = task_queue().get(task_id)
    except TaskNotFound:
        return jsonify({"error": "task not found", "code": "task_not_found"}), 404
    except QueueError as exc:
        return jsonify({"error": str(exc), "code": "queue_unavailable"}), 503
    public_task = public_task_payload(task)
    return jsonify({"status": public_task["status"], "image_url": public_task["image_url"]})


@app.errorhandler(404)
def not_found(_error: Any):
    return jsonify({"error": "not found", "code": "not_found"}), 404
