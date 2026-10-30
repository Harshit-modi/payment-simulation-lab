"""Batch settlement simulator: process one shard of an input file, write a result file.

Same image as the simulator, different command:  python batch.py
Run as a Kubernetes Indexed Job: each Pod gets JOB_COMPLETION_INDEX and handles the
lines where (line_number % SHARDS) == index, so N Pods split one file without overlap.

Input  (INPUT_FILE):  CSV  merchant_ref,amount
Output (OUTPUT_DIR):  results-part-<index>.csv  merchant_ref,amount,status,response_code,processed_by
"""

import csv
import os
import socket
import sys
import time

index = int(os.environ.get("JOB_COMPLETION_INDEX", "0"))
shards = int(os.environ.get("SHARDS", "1"))
input_file = os.environ.get("INPUT_FILE", "/input/payments.csv")
output_dir = os.environ.get("OUTPUT_DIR", "/output")
delay = float(os.environ.get("PER_ITEM_DELAY_SECONDS", "0.2"))  # makes parallelism visible

if not 0 <= index < shards:
    sys.exit(f"JOB_COMPLETION_INDEX {index} is outside 0..{shards - 1}")

with open(input_file, newline="") as f:
    rows = [r for r in csv.DictReader(f)]
mine = [r for i, r in enumerate(rows) if i % shards == index]

os.makedirs(output_dir, exist_ok=True)
part = os.path.join(output_dir, f"results-part-{index}.csv")
tmp = part + ".tmp"
with open(tmp, "w", newline="") as f:
    out = csv.writer(f)
    out.writerow(["merchant_ref", "amount", "status", "response_code", "processed_by"])
    for r in mine:
        time.sleep(delay)
        declined = int(r["amount"]) % 100 == 99  # same rule as the online API
        out.writerow([r["merchant_ref"], r["amount"], "DECLINED" if declined else "SETTLED",
                      "05" if declined else "00", socket.gethostname()])
os.replace(tmp, part)  # atomic: readers never see a half-written result file
print(f"shard {index}/{shards}: processed {len(mine)} of {len(rows)} rows -> {part}", flush=True)
