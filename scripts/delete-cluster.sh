#!/usr/bin/env bash
# Delete ONLY the payment-lab kind cluster. Asks for confirmation unless --yes.
# Other kind clusters, Docker images/volumes, and ~/.kube/config are not touched.
set -euo pipefail

source "$(dirname "$0")/env.sh"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "Cluster '$CLUSTER_NAME' does not exist. Nothing to do."
  exit 0
fi

if [[ "${1:-}" != "--yes" ]]; then
  echo "This permanently deletes kind cluster '$CLUSTER_NAME' and everything running in it."
  read -r -p "Type the cluster name to confirm: " answer
  [[ "$answer" == "$CLUSTER_NAME" ]] || { echo "Aborted."; exit 1; }
fi

# --kubeconfig makes kind remove the context from the project file only.
kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$LAB_KUBECONFIG"
rm -f "$LAB_KUBECONFIG"
echo "Deleted '$CLUSTER_NAME'. Recreate with: scripts/create-cluster.sh"
