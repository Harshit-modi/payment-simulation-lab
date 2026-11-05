#!/usr/bin/env bash
# Smoke-test the payment-lab cluster in a throwaway namespace, then delete it.
set -euo pipefail

source "$(dirname "$0")/env.sh"

NS="lab-verify"
K=(kubectl --kubeconfig "$LAB_KUBECONFIG" --context "$KUBE_CONTEXT")
H=(helm --kubeconfig "$LAB_KUBECONFIG" --kube-context "$KUBE_CONTEXT")
TMP_CHART_DIR="$(mktemp -d)"

cleanup() {
  "${K[@]}" delete namespace "$NS" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  rm -rf "$TMP_CHART_DIR"
}
trap cleanup EXIT

step() { printf '\n==> %s\n' "$*"; }

step "1/8 Context is the lab cluster"
"${K[@]}" config current-context
"${K[@]}" cluster-info | head -1

step "2/8 All nodes Ready"
"${K[@]}" wait --for=condition=Ready nodes --all --timeout=120s
"${K[@]}" get nodes

step "3/8 System Pods healthy (CoreDNS, kube-proxy, CNI, control plane)"
"${K[@]}" -n kube-system rollout status deployment/coredns --timeout=120s
"${K[@]}" -n kube-system rollout status daemonset/kube-proxy --timeout=120s
"${K[@]}" -n kube-system rollout status daemonset/kindnet --timeout=120s
"${K[@]}" -n kube-system wait --for=condition=Ready pods --all --timeout=120s
"${K[@]}" -n kube-system get pods -o wide

step "4/8 Temporary Deployment starts"
"${K[@]}" create namespace "$NS"
"${K[@]}" -n "$NS" create deployment web --image=nginx:1.27-alpine --replicas=2
"${K[@]}" -n "$NS" rollout status deployment/web --timeout=180s
"${K[@]}" -n "$NS" get pods -o wide

step "5/8 ClusterIP Service selects the Deployment's Pods"
"${K[@]}" -n "$NS" expose deployment web --port=80 --target-port=80 --type=ClusterIP
"${K[@]}" -n "$NS" get service web
"${K[@]}" -n "$NS" get endpointslices -l kubernetes.io/service-name=web

step "6/8 Client Pod resolves the Service by DNS and gets HTTP 200"
"${K[@]}" -n "$NS" run client --image=busybox:1.36 --restart=Never --command -- \
  sh -c 'nslookup web.lab-verify.svc.cluster.local && wget -S -qO- http://web.lab-verify.svc.cluster.local 2>&1 | grep -E "HTTP/|<title>"'
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/client --timeout=120s \
  || { "${K[@]}" -n "$NS" logs client || true; exit 1; }
"${K[@]}" -n "$NS" logs client

step "7/8 Helm talks to the cluster (install, list, uninstall a local chart)"
"${H[@]}" version --short
helm create "$TMP_CHART_DIR/hello" >/dev/null
"${H[@]}" install hello "$TMP_CHART_DIR/hello" -n "$NS" --set image.repository=nginx --set image.tag=1.27-alpine --wait --timeout 3m
"${H[@]}" list -n "$NS"
"${H[@]}" uninstall hello -n "$NS" --wait

step "8/8 NetworkPolicy is ENFORCED (canary: denied traffic must not get through)"
# Step 6 proved web is reachable. Deny all ingress to it; the same call must now fail.
# Creating policy objects proves nothing: only a blocked connection proves enforcement.
"${K[@]}" -n "$NS" apply -f - <<'NETPOL'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: canary-deny-web
spec:
  podSelector:
    matchLabels:
      app: web
  policyTypes: [Ingress]
NETPOL
# sleep first: kindnet doesn't police a Pod in its first moment, so an instant
# connection would measure startup timing instead of the policy.
"${K[@]}" -n "$NS" run canary --image=busybox:1.36 --restart=Never --command -- \
  sh -c 'sleep 3; if wget -qO- -T 3 http://web.lab-verify.svc.cluster.local >/dev/null 2>&1; then echo LEAKED; else echo BLOCKED; fi'
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/canary --timeout=120s
canary_result=$("${K[@]}" -n "$NS" logs canary)
echo "canary: $canary_result"
if [[ "$canary_result" != "BLOCKED" ]]; then
  echo "FAIL: NetworkPolicy is not enforced. Try: kubectl -n kube-system rollout restart ds/kindnet" >&2
  exit 1
fi

step "All checks passed. Removing namespace $NS."
