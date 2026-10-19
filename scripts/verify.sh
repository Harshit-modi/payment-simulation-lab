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

step "1/7 Context is the lab cluster"
"${K[@]}" config current-context
"${K[@]}" cluster-info | head -1

step "2/7 All nodes Ready"
"${K[@]}" wait --for=condition=Ready nodes --all --timeout=120s
"${K[@]}" get nodes

step "3/7 System Pods healthy (CoreDNS, kube-proxy, CNI, control plane)"
"${K[@]}" -n kube-system rollout status deployment/coredns --timeout=120s
"${K[@]}" -n kube-system rollout status daemonset/kube-proxy --timeout=120s
"${K[@]}" -n kube-system rollout status daemonset/kindnet --timeout=120s
"${K[@]}" -n kube-system wait --for=condition=Ready pods --all --timeout=120s
"${K[@]}" -n kube-system get pods -o wide

step "4/7 Temporary Deployment starts"
"${K[@]}" create namespace "$NS"
"${K[@]}" -n "$NS" create deployment web --image=nginx:1.27-alpine --replicas=2
"${K[@]}" -n "$NS" rollout status deployment/web --timeout=180s
"${K[@]}" -n "$NS" get pods -o wide

step "5/7 ClusterIP Service selects the Deployment's Pods"
"${K[@]}" -n "$NS" expose deployment web --port=80 --target-port=80 --type=ClusterIP
"${K[@]}" -n "$NS" get service web
"${K[@]}" -n "$NS" get endpointslices -l kubernetes.io/service-name=web

step "6/7 Client Pod resolves the Service by DNS and gets HTTP 200"
"${K[@]}" -n "$NS" run client --image=busybox:1.36 --restart=Never --command -- \
  sh -c 'nslookup web.lab-verify.svc.cluster.local && wget -S -qO- http://web.lab-verify.svc.cluster.local 2>&1 | grep -E "HTTP/|<title>"'
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/client --timeout=120s \
  || { "${K[@]}" -n "$NS" logs client || true; exit 1; }
"${K[@]}" -n "$NS" logs client

step "7/7 Helm talks to the cluster (install, list, uninstall a local chart)"
"${H[@]}" version --short
helm create "$TMP_CHART_DIR/hello" >/dev/null
"${H[@]}" install hello "$TMP_CHART_DIR/hello" -n "$NS" --set image.repository=nginx --set image.tag=1.27-alpine --wait --timeout 3m
"${H[@]}" list -n "$NS"
"${H[@]}" uninstall hello -n "$NS" --wait

step "All checks passed. Removing namespace $NS."
