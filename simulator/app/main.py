"""Minimal payment processor simulator.

Payment logic is deliberately small; the point is the Kubernetes behavior around it.

State lives in Redis (REDIS_URL) so every replica sees the same payments and
idempotency keys, and state survives Pod restarts and rollouts.

Processor behavior (latency, error and throttle rates) comes from a JSON profile
at PROFILE_PATH, read ONCE at startup: changing behavior means a new versioned
ConfigMap and a rollout, so each Pod serves exactly one known profile version.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import random
import socket
import urllib.request
import uuid

import redis
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

POD = socket.gethostname()

# Fail fast: a missing or invalid profile crashes startup (CrashLoopBackOff) instead
# of silently serving default behavior that no test asked for.
_profile_raw = open(os.environ.get("PROFILE_PATH", "/etc/simulator/profile.json"), "rb").read()
PROFILE = json.loads(_profile_raw)
for _field in ("processor", "version", "latency_ms", "error_rate", "throttle_rate"):
    if _field not in PROFILE:
        raise SystemExit(f"profile is missing required field: {_field}")
PROFILE_HASH = hashlib.sha256(_profile_raw).hexdigest()[:12]
PROCESSOR = PROFILE["processor"]
IDEMPOTENCY_TTL = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))

store = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                             decode_responses=True, socket_timeout=2, socket_connect_timeout=2)

# Callbacks pending in THIS process. They live only in memory: if the Pod stops
# before they're sent, they're lost. Shutdown waits up to SHUTDOWN_DRAIN_SECONDS.
pending_callbacks: set[asyncio.Task] = set()
SHUTDOWN_DRAIN_SECONDS = float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "25"))


@contextlib.asynccontextmanager
async def lifespan(_app):
    yield
    if pending_callbacks:
        print(f"shutdown: draining {len(pending_callbacks)} pending callbacks "
              f"(up to {SHUTDOWN_DRAIN_SECONDS:.0f}s)", flush=True)
        done, not_done = await asyncio.wait(pending_callbacks, timeout=SHUTDOWN_DRAIN_SECONDS)
        print(f"shutdown: delivered {len(done)}, DROPPED {len(not_done)} callbacks", flush=True)


app = FastAPI(title="payment-simulator", lifespan=lifespan)

TRANSITIONS = {"capture": ("AUTHORIZED", "CAPTURED"), "refund": ("CAPTURED", "REFUNDED")}


def reply(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body,
                        headers={"X-Served-By": POD, "X-Processor": PROCESSOR,
                                 "X-Profile-Version": f"{PROFILE['version']}@{PROFILE_HASH}"})


def error(status: int, code: str, **extra) -> JSONResponse:
    return reply(status, {"error": code, **extra})


@app.exception_handler(redis.RedisError)
async def store_unavailable(request: Request, exc: redis.RedisError):
    # Fail fast with a clear error instead of going unready: a shared dependency
    # being down shouldn't empty the Service's endpoints (see lab 02.2).
    return error(503, "store_unavailable", detail=type(exc).__name__)


async def simulate_processor(forced: str | None) -> JSONResponse | None:
    """Apply profile latency and faults. X-Simulate forces one: throttle | error | timeout."""
    lat = PROFILE["latency_ms"]
    await asyncio.sleep(random.uniform(lat["min"], lat["max"]) / 1000)
    if forced == "timeout":
        await asyncio.sleep(PROFILE.get("timeout_seconds", 30))
        return error(504, "processor_timeout")
    if forced == "throttle" or (forced is None and random.random() < PROFILE["throttle_rate"]):
        resp = error(429, "throttled")
        resp.headers["Retry-After"] = str(PROFILE.get("retry_after_seconds", 1))
        return resp
    if forced == "error" or (forced is None and random.random() < PROFILE["error_rate"]):
        return error(503, "processor_unavailable")
    return None


@app.get("/v1/profile")
def profile():
    return reply(200, {"profile": PROFILE, "hash": PROFILE_HASH})


def _post_json(url: str, body: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


async def deliver_callback(url: str, event: dict):
    """Wait the profile's callback delay, then POST the event, retrying a few times."""
    delay = PROFILE.get("callback_delay_ms", {"min": 1000, "max": 3000})
    await asyncio.sleep(random.uniform(delay["min"], delay["max"]) / 1000)
    copies = 2 if random.random() < PROFILE.get("callback_duplicate_rate", 0.0) else 1
    for copy in range(copies):
        for attempt in range(3):
            try:
                status = await asyncio.to_thread(_post_json, url, {**event, "delivery": copy + 1})
                print(f"callback {event['event_id']} delivery={copy + 1} -> {status}", flush=True)
                break
            except Exception as exc:  # receiver down or slow: back off and retry
                print(f"callback {event['event_id']} attempt {attempt + 1} failed: {exc}", flush=True)
                await asyncio.sleep(2 ** attempt)


def schedule_callback(url: str, run_id: str, payment: dict):
    event = {"event_id": f"evt_{uuid.uuid4().hex[:12]}", "type": "payment.authorized",
             "run_id": run_id, "payment_id": payment["id"], "status": payment["status"],
             "sent_by": POD}
    task = asyncio.get_running_loop().create_task(deliver_callback(url, event))
    pending_callbacks.add(task)
    task.add_done_callback(pending_callbacks.discard)


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
                    idempotency_key: str | None = Header(None),
                    x_simulate: str | None = Header(None)):
    # Faults run before the idempotency claim: transient failures are never
    # stored, so a client retry with the same key is processed again.
    if fault := await simulate_processor(x_simulate):
        return fault
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
        if payload.get("callback_url"):
            schedule_callback(payload["callback_url"], x_test_run_id, payment)
        return reply(201, payment)

    return await idempotent(request, x_test_run_id, idempotency_key, handler)


@app.post("/v1/payments/{payment_id}/{action}")
async def transition(payment_id: str, action: str, request: Request,
                     x_test_run_id: str = Header(...),
                     idempotency_key: str | None = Header(None),
                     x_simulate: str | None = Header(None)):
    if action not in TRANSITIONS:
        return error(404, "unknown_action")
    if fault := await simulate_processor(x_simulate):
        return fault

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
