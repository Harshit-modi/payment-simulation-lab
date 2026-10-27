"""Callback receiver: a stand-in "merchant" that records the webhooks it receives.

Same image as the simulator, different command:  uvicorn receiver:app --port 8080
Events are kept in memory per run (single replica; it's a test fixture).
"""

import collections
import socket

from fastapi import FastAPI, Request

app = FastAPI(title="callback-receiver")
events: dict[str, list[dict]] = collections.defaultdict(list)


@app.get("/livez")
def livez():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {"status": "ready"}


@app.post("/webhook")
async def webhook(request: Request):
    event = await request.json()
    events[event.get("run_id", "unknown")].append(event)
    print(f"received {event.get('event_id')} delivery={event.get('delivery')} "
          f"payment={event.get('payment_id')}", flush=True)
    return {"received": True, "by": socket.gethostname()}


@app.get("/events")
def list_events(run_id: str):
    run_events = events.get(run_id, [])
    unique = {e["event_id"] for e in run_events}
    return {"run_id": run_id, "received": len(run_events), "unique_events": len(unique),
            "duplicates": len(run_events) - len(unique), "events": run_events}
