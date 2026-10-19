#!/usr/bin/env bash
# Create the payment-lab kind cluster, or reuse it if it already exists.
# Safe to rerun: an existing cluster is never deleted or modified here.
set -euo pipefail

source "$(dirname "$0")/env.sh"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not reachable. Start the runtime first:  colima start" >&2
  exit 1
fi

mkdir -p "$(dirname "$LAB_KUBECONFIG")"
chmod 700 "$(dirname "$LAB_KUBECONFIG")"

expected_nodes=$(grep -cE '^\s*-\s*role:' "$KIND_CONFIG")

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  actual_nodes=$(kind get nodes --name "$CLUSTER_NAME" | wc -l | tr -d ' ')
  echo "Cluster '$CLUSTER_NAME' already exists ($actual_nodes nodes); reusing it."
  if [[ "$actual_nodes" != "$expected_nodes" ]]; then
    echo "WARNING: config expects $expected_nodes nodes but cluster has $actual_nodes." >&2
    echo "         Not changing anything. To rebuild from config, run:" >&2
    echo "         scripts/delete-cluster.sh && scripts/create-cluster.sh" >&2
    exit 1
  fi
  # Refresh the project kubeconfig (e.g. if it was removed). Writes only to $LAB_KUBECONFIG.
  kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$LAB_KUBECONFIG"
else
  echo "Creating cluster '$CLUSTER_NAME' from $KIND_CONFIG ..."
  kind create cluster \
    --name "$CLUSTER_NAME" \
    --config "$KIND_CONFIG" \
    --kubeconfig "$LAB_KUBECONFIG" \
    --wait 5m
fi

chmod 600 "$LAB_KUBECONFIG"

# After a runtime restart the API server can briefly return Forbidden while
# RBAC caches warm up, so wait for /readyz before talking to it.
echo "Waiting for API server readiness ..."
for _ in $(seq 1 60); do
  kubectl --kubeconfig "$LAB_KUBECONFIG" --context "$KUBE_CONTEXT" \
    get --raw='/readyz' >/dev/null 2>&1 && break
  sleep 3
done

kubectl --kubeconfig "$LAB_KUBECONFIG" --context "$KUBE_CONTEXT" \
  wait --for=condition=Ready nodes --all --timeout=180s
kubectl --kubeconfig "$LAB_KUBECONFIG" --context "$KUBE_CONTEXT" get nodes -o wide

echo
echo "Done. In your shell run:  source scripts/env.sh"
