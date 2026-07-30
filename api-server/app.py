from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.redis_queue import QueueError, TaskNotFound, public_task_payload, queue_from_env
from shared.prompt_limits import (
    MAX_PROMPT_REQUEST_BODY_BYTES,
    PromptValidationError,
    normalize_prompt,
)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PROMPT_REQUEST_BODY_BYTES


def prompt_api_token() -> str:
    return str(os.environ.get("PROMPT_API_TOKEN") or "").strip()


def prompt_api_auth_error():
    expected = prompt_api_token()
    if not expected:
        return jsonify(
            {
                "error": "generation API authentication is unavailable",
                "code": "prompt_api_auth_unavailable",
            }
        ), 503

    supplied = str(request.headers.get("X-Prompt-API-Token") or "").strip()
    authorization = str(request.headers.get("Authorization") or "").strip()
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        return jsonify(
            {
                "error": "generation API authentication is required",
                "code": "prompt_api_auth_required",
            }
        ), 401
    if not hmac.compare_digest(supplied, expected):
        return jsonify(
            {
                "error": "generation API authentication failed",
                "code": "prompt_api_auth_forbidden",
            }
        ), 403
    return None


def task_queue():
    return queue_from_env()


@app.get("/healthz")
def healthz():
    service_id = (
        str(os.environ.get("PROMPT_WORKER_SERVICE_ID") or "prompt-worker").strip()
        or "prompt-worker"
    )
    try:
        worker_liveness = task_queue().service_liveness(service_id)
    except QueueError:
        return jsonify(
            {
                "ok": False,
                "service": "api-server",
                "code": "queue_unavailable",
            }
        ), 503
    if worker_liveness is None:
        return jsonify(
            {
                "ok": False,
                "service": "api-server",
                "code": "prompt_worker_unavailable",
            }
        ), 503
    return jsonify(
        {
            "ok": True,
            "service": "api-server",
            "promptWorker": "ready",
        }
    )


@app.post("/generate")
def generate():
    auth_error = prompt_api_auth_error()
    if auth_error is not None:
        return auth_error
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object", "code": "invalid_request"}), 400
    try:
        prompt = normalize_prompt(payload.get("prompt"))
    except PromptValidationError as exc:
        return jsonify(
            {
                "error": str(exc),
                "code": "invalid_generation_request",
            }
        ), 400
    try:
        task = task_queue().enqueue(
            {"taskType": "prompt_generation", "prompt": prompt}
        )
    except QueueError as exc:
        return jsonify({"error": str(exc), "code": "queue_unavailable"}), 503
    return jsonify({"task_id": task["task_id"]}), 202


@app.get("/status/<task_id>")
def status(task_id: str):
    auth_error = prompt_api_auth_error()
    if auth_error is not None:
        return auth_error
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


@app.errorhandler(413)
def request_too_large(_error: Any):
    return jsonify(
        {
            "error": "generation request exceeds size limit",
            "code": "generation_request_too_large",
        }
    ), 413
