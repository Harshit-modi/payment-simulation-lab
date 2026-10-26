# Simulator Dockerfile
# Base pinned by digest so rebuilds use the exact same OS and Python layers.
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962

# unbuffered output so `kubectl logs` shows lines immediately; no .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# run as a non-root user (created before COPY so code changes don't rebuild this layer)
RUN useradd --uid 10001 --no-create-home appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# code stays root-owned: the app user can read it but not modify it
COPY app/ .

USER 10001
EXPOSE 8080
# exec form: uvicorn is PID 1 and receives SIGTERM directly
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
