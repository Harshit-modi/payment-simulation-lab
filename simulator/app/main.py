"""Minimal payment processor simulator.

Payment logic is deliberately small; the point is the Kubernetes behavior around it.

State lives in process memory ON PURPOSE: with more than one replica, payments
and idempotency keys are split across Pods. Lab 2.4 fixes that with a shared store.
"""

import hashlib
import json
import os
import socket
import uuid

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

PROCESSOR = os.environ.get("PROCESSOR_NAME", "acme-pay")
POD = socket.gethostname()

app = FastAPI(title="payment-simulator")

# (run_id, payment_id) -> payment
payments: dict[tuple[str, str], dict] = {}
# (run_id, idempotency_key) -> {"body_hash", "status", "body"}
idempotency: dict[tuple[str, str], dict] = {}

TRANSITIONS = {"capture": ("AUTHORIZED", "CAPTURED"), "refund": ("CAPTURED", "REFUNDED")}


def reply(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body,
                        headers={"X-Served-By": POD, "X-Processor": PROCESSOR})


def error(status: int, code: str, **extra) -> JSONResponse:
    return reply(status, {"error": code, **extra})


async def idempotent(request: Request, run_id: str, key: str | None, handler):
    """Replay the stored response for a repeated key; reject a reused key with a different body."""
    if not key:
        return handler()
    body_hash = hashlib.sha256(await request.body()).hexdigest()
    stored = idempotency.get((run_id, key))
    if stored:
        if stored["body_hash"] != body_hash:
            return error(422, "idempotency_key_reused", detail="same key, different request body")
        resp = reply(stored["status"], stored["body"])
        resp.headers["Idempotent-Replayed"] = "true"
        return resp
    resp = handler()
    idempotency[(run_id, key)] = {"body_hash": body_hash, "status": resp.status_code,
                                  "body": json.loads(resp.body)}
    return resp


@app.get("/livez")
def livez():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready"}


@app.post("/v1/payments/authorize")
async def authorize(request: Request,
                    x_test_run_id: str = Header(...),
                    idempotency_key: str | None = Header(None)):
    payload = await request.json()
    if not isinstance(payload.get("amount"), int) or payload["amount"] <= 0:
        return error(400, "invalid_request", detail="amount must be a positive integer (minor units)")

    def handler():
        # Simple deterministic rule for tests: amounts ending in 99 are declined.
        declined = payload["amount"] % 100 == 99
        payment = {"id": f"pay_{uuid.uuid4().hex[:12]}", "processor": PROCESSOR,
                   "amount": payload["amount"], "currency": payload.get("currency", "USD"),
                   "status": "DECLINED" if declined else "AUTHORIZED",
                   "responseCode": "05" if declined else "00"}
        payments[(x_test_run_id, payment["id"])] = payment
        return reply(201, payment)

    return await idempotent(request, x_test_run_id, idempotency_key, handler)


@app.post("/v1/payments/{payment_id}/{action}")
async def transition(payment_id: str, action: str, request: Request,
                     x_test_run_id: str = Header(...),
                     idempotency_key: str | None = Header(None)):
    if action not in TRANSITIONS:
        return error(404, "unknown_action")

    def handler():
        payment = payments.get((x_test_run_id, payment_id))
        if not payment:
            return error(404, "payment_not_found")
        required, target = TRANSITIONS[action]
        if payment["status"] != required:
            return error(409, "invalid_state_transition", current=payment["status"], requires=required)
        payment["status"] = target
        return reply(200, payment)

    return await idempotent(request, x_test_run_id, idempotency_key, handler)


@app.get("/v1/payments/{payment_id}")
def get_payment(payment_id: str, x_test_run_id: str = Header(...)):
    payment = payments.get((x_test_run_id, payment_id))
    return reply(200, payment) if payment else error(404, "payment_not_found")
