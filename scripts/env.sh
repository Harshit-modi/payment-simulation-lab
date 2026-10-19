# Shared settings for payment-lab scripts.
#
# Source it in your shell to point kubectl/helm at this project only:
#   source scripts/env.sh
#
# It sets KUBECONFIG for the current shell only. ~/.kube/config is never touched.

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/.." && pwd)"
export LAB_ROOT
export CLUSTER_NAME="payment-lab"
export KUBE_CONTEXT="kind-${CLUSTER_NAME}"
export KIND_CONFIG="${LAB_ROOT}/kind/payment-lab.yaml"
export LAB_KUBECONFIG="${LAB_ROOT}/.kube/payment-lab.kubeconfig"
export KUBECONFIG="${LAB_KUBECONFIG}"
# kind should always use Docker (served by Colima).
export KIND_EXPERIMENTAL_PROVIDER="docker"
