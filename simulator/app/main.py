"""Minimal payment processor simulator.

Payment logic is deliberately small; the point is the Kubernetes behavior around it.

State lives in Redis (REDIS_URL) so every replica sees the same payments and
idempotency keys, and state survives Pod restarts and rollouts.
"""

import hashlib
import json
import os
import socket
import uuid

import redis
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

PROCESSOR = os.environ.get("PROCESSOR_NAME", "acme-pay")
POD = socket.gethostname()
IDEMPOTENCY_TTL = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))

store = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                             decode_responses=True, socket_timeout=2, socket_connect_timeout=2)

app = FastAPI(title="payment-simulator")

TRANSITIONS = {"capture": ("AUTHORIZED", "CAPTURED"), "refund": ("CAPTURED", "REFUNDED")}


def reply(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body,
                        headers={"X-Served-By": POD, "X-Processor": PROCESSOR})


def error(status: int, code: str, **extra) -> JSONResponse:
    return reply(status, {"error": code, **extra})


@app.exception_handler(redis.RedisError)
async def store_unavailable(request: Request, exc: redis.RedisError):
    # Fail fast with a clear error instead of going unready: a shared dependency
    # being down shouldn't empty the Service's endpoints (see lab 02.2).
    return error(503, "store_unavailable", detail=type(exc).__name__)


def payment_key(run_id: str, payment_id: str) -> str:
    return f"pay:{run_id}:{payment_id}"


async def idempotent(request: Request, run_id: str, key: str | None, handler):
    """Replay the stored response for a repeated key; reject a reused key with a different body.

    SET NX claims the key atomically, so of two concurrent duplicates exactly one
    processes the request and the other gets 409 request_in_progress.
    """
    if not key:
        return handler()
    body_hash = hashlib.sha256(await request.body()).hexdigest()
    redis_key = f"idem:{run_id}:{key}"
    claimed = store.set(redis_key, json.dumps({"body_hash": body_hash, "state": "processing"}),
                        nx=True, ex=IDEMPOTENCY_TTL)
    if not claimed:
        stored = json.loads(store.get(redis_key) or "{}")
        if stored.get("body_hash") != body_hash:
            return error(422, "idempotency_key_reused", detail="same key, different request body")
        if stored.get("state") == "processing":
            return error(409, "request_in_progress")
        resp = reply(stored["status"], stored["body"])
        resp.headers["Idempotent-Replayed"] = "true"
        return resp
    try:
        resp = handler()
    except Exception:
        store.delete(redis_key)  # let a retry process again instead of replaying a crash
        raise
    store.set(redis_key, json.dumps({"body_hash": body_hash, "state": "done",
                                     "status": resp.status_code, "body": json.loads(resp.body)}),
              ex=IDEMPOTENCY_TTL)
    return resp


@app.get("/livez")
def livez():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    # Only this process's own readiness; the shared store is not checked on purpose.
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
        store.set(payment_key(x_test_run_id, payment["id"]), json.dumps(payment))
        return reply(201, payment)

    return await idempotent(request, x_test_run_id, idempotency_key, handler)


@app.post("/v1/payments/{payment_id}/{action}")
async def transition(payment_id: str, action: str, request: Request,
                     x_test_run_id: str = Header(...),
                     idempotency_key: str | None = Header(None)):
    if action not in TRANSITIONS:
        return error(404, "unknown_action")

    def handler():
        key = payment_key(x_test_run_id, payment_id)
        required, target = TRANSITIONS[action]
        # WATCH/MULTI: the state check and update are atomic across replicas.
        with store.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        return error(404, "payment_not_found")
                    payment = json.loads(raw)
                    if payment["status"] != required:
                        return error(409, "invalid_state_transition",
                                     current=payment["status"], requires=required)
                    payment["status"] = target
                    pipe.multi()
                    pipe.set(key, json.dumps(payment))
                    pipe.execute()
                    return reply(200, payment)
                except redis.WatchError:
                    continue  # another replica changed it first; re-read and re-check

    return await idempotent(request, x_test_run_id, idempotency_key, handler)


@app.get("/v1/payments/{payment_id}")
def get_payment(payment_id: str, x_test_run_id: str = Header(...)):
    raw = store.get(payment_key(x_test_run_id, payment_id))
    return reply(200, json.loads(raw)) if raw else error(404, "payment_not_found")
