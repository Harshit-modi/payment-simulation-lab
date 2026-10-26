#!/bin/bash
# Create or update the acme-pay-credentials Secret from $LAB_ROOT/.env.local
# (git-ignored). The value never appears on the command line or in shell history.
#
# .env.local format:
#   api-key=<value>
set -euo pipefail

source "$(dirname "$0")/env.sh"

ENV_FILE="${LAB_ROOT}/.env.local"

# 1. read api-key if it exists or fail
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: $ENV_FILE file not found."
  exit 1
fi

# read var or fail (an empty value counts as missing)
if ! grep -q '^api-key=.\+' "$ENV_FILE"; then
  echo "Error: api-key not found or empty in $ENV_FILE."
  exit 1
fi

# keep the file readable only by its owner
chmod 600 "$ENV_FILE"

# 2. create or update the secret in lab01, always targeting the lab cluster
kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" create secret generic acme-pay-credentials \
  --from-env-file="$ENV_FILE" \
  --namespace=lab01 \
  --dry-run=client -o yaml \
  | kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" apply -f -
