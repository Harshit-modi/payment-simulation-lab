# payment-simulation-lab

A local Kubernetes workspace for learning Kubernetes fundamentals, Helm, multi-tenancy, and troubleshooting.
The goal is to integration-test a payment processor here and later deploy the same app to EKS.

The payment simulator itself is not built yet. We build it during the labs.

## Layout

| Path | Purpose |
|---|---|
| `kind/payment-lab.yaml` | Cluster definition, the source of truth for topology and Kubernetes version |
| `scripts/env.sh` | Shared settings. `source` it to point kubectl and Helm at this cluster only |
| `scripts/create-cluster.sh` | Creates the cluster, or reuses it if it already exists. Safe to rerun |
| `scripts/verify.sh` | End-to-end smoke test in a throwaway `lab-verify` namespace |
| `scripts/delete-cluster.sh` | Deletes **only** `payment-lab`, after you confirm |
| `labs/01-foundations/` | First lab |
| `charts/` | Helm charts, added later |
| `.kube/` | Project kubeconfig, created by the scripts and **git-ignored** |

## Installed toolchain (macOS 15.5, Apple Silicon, 16 GiB RAM)

Installed with Homebrew on 2026-10-17:

| Tool | Version | Role |
|---|---|---|
| Colima | 0.10.3 | Runs a small Linux VM (Apple `vz` hypervisor) that hosts the Docker engine |
| Docker CLI / Engine | 29.9.1 / 29.5.2 | Container runtime that kind drives; the engine runs inside the Colima VM |
| kind | v0.33.0 | Creates Kubernetes clusters whose "nodes" are Docker containers |
| kubectl | v1.37.1 | Kubernetes CLI. Within the supported ±1 minor-version skew of the v1.37.0 cluster |
| Helm | v4.3.1 | Kubernetes package manager (Helm 4 is the current major version) |
| Git | 2.39.5 (Apple) | Pre-installed, reused |
| Kubernetes node image | `kindest/node:v1.37.0` (pinned by digest) | Default image for kind v0.33.0 |

Colima VM resources: **4 CPU, 8 GiB RAM, 60 GiB disk**. kind recommends at least 8 GB for the Docker VM on macOS,
which still leaves about 8 GiB for macOS itself.

## Architecture

```
 macOS host (one physical machine)
 └── Colima VM (Linux, 4 CPU / 8 GiB)
     └── Docker engine
         ├── container: payment-lab-control-plane  ← API server, etcd, scheduler, controller-manager, CoreDNS
         ├── container: payment-lab-worker         ← kubelet + containerd, runs your Pods
         └── container: payment-lab-worker2        ← kubelet + containerd, runs your Pods
              (CNI: kindnet · Service proxy: kube-proxy · storage: local-path-provisioner)
```

These are **real Kubernetes nodes**: each container runs a real kubelet and containerd, and they join with kubeadm,
just like nodes on EC2. They are not separate machines, though. All three share the Colima VM's CPU, memory, disk,
and kernel. So "node failure" or "node capacity" exercises here are simulations. A node going down does not protect
you from the laptop going down.

The API server is published on `127.0.0.1:<random port>`, so it is reachable only from this Mac.

## Why these choices

- **Colima instead of Docker Desktop.** It's open source, CLI-driven, and scriptable (`colima start`), with no
  licensing terms to think about. kind only needs a Docker-compatible socket, and Colima provides one.
- **1 control plane + 2 workers.** Two workers let you see scheduling, spreading, node affinity/taints, and draining.
  Resources were sufficient, so nothing was scaled down.
- **Separate kubeconfig (`.kube/payment-lab.kubeconfig`).** kind would normally merge credentials into
  `~/.kube/config` and switch your current context. Passing `--kubeconfig` to kind and pointing `KUBECONFIG` at the
  project file keeps this lab isolated. Every script also passes `--kubeconfig` and `--context kind-payment-lab`
  explicitly, so a script can never act on another cluster.
- **Node image pinned by digest.** Recreating the cluster gives the same Kubernetes build, as the kind release
  notes recommend.
- **Nothing extra installed.** No ingress, GitOps, monitoring, or service mesh yet. Each gets added in a lab, when
  you can see what it changes.

## Daily use

All commands are run from the repository root.

```sh
cd ~/workspace/payment-simulation-lab

# 1. Start the container runtime (VM settings are remembered after the first start)
colima start
colima status

# 2. Select the project kubeconfig (current shell only; ~/.kube/config is untouched)
source scripts/env.sh
kubectl config current-context        # → kind-payment-lab

# 3. Check nodes and Pods
kubectl get nodes -o wide
kubectl get pods -A

# 4. Reuse the existing cluster the next day (does not delete anything; waits for readiness)
./scripts/create-cluster.sh

# Optional: full smoke test
./scripts/verify.sh

# When done for the day (cluster state is kept)
colima stop
```

The first `colima start` used `colima start --cpu 4 --memory 8 --disk 60 --vm-type vz`. To change those settings
later, run `colima stop`, then `colima start --cpu N --memory N`.

Optional: `brew services start colima` starts Colima automatically at login. It is not enabled now.

### Recreate the cluster from its saved configuration

```sh
./scripts/delete-cluster.sh      # asks you to type "payment-lab"
./scripts/create-cluster.sh      # rebuilds from kind/payment-lab.yaml
kubectl apply -f labs/...        # reapply whatever the labs need
```

### Delete only this practice cluster

```sh
./scripts/delete-cluster.sh            # interactive confirmation
./scripts/delete-cluster.sh --yes      # non-interactive
```

This runs `kind delete cluster --name payment-lab --kubeconfig .kube/payment-lab.kubeconfig`. It removes the three
node containers and the project kubeconfig. Other kind clusters, Docker images and volumes, the Colima VM, and
`~/.kube/config` are not touched.

## What survives, and what doesn't

| Event | Result |
|---|---|
| `colima stop` / `colima start`, Mac reboot | **Cluster survives** (tested). Node containers restart and the etcd data inside them is kept, so Deployments, Services, Secrets, and PVC data come back. Node IPs on the Docker network can change, so never hardcode them. The API server may return `Forbidden` for a few seconds while starting; `create-cluster.sh` waits for `/readyz`. |
| `scripts/delete-cluster.sh` | **Everything inside Kubernetes is lost:** all objects, etcd, PV data, and images loaded with `kind load`. The repository is untouched. |
| `colima delete` (VM reset) | The VM disk is wiped, so the cluster, all Docker images, and all volumes are lost. Avoid it unless you mean it. |
| Brew upgrade of kind or Colima | Usually fine. Recreate the cluster if kind's node image or config format changes. |

**The repository is the source of truth.** Anything you create with ad-hoc `kubectl create` or `kubectl edit`
exists only in the cluster. Write manifests and charts into `labs/` and `charts/`, and commit them, so a recreated
cluster can be rebuilt with `kubectl apply` or `helm install`.

## NetworkPolicy note

The CNI is **kindnet**. In kind v0.33.0 it runs the `kube-network-policies` controller, and the kindnet logs show
"Policy engine is ready", so policies should be enforced. Still, creating a `NetworkPolicy` object only proves the
API accepted it. A CNI without policy support silently ignores policies. Before relying on policies in the
multi-tenancy labs, prove enforcement: show that a connection succeeds, apply a default-deny policy, and show that
the same connection now fails.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| `Cannot connect to the Docker daemon` | `colima status`, then `colima start`. Check `docker context ls`: `colima` should be marked `*`. |
| `kubectl` talks to the wrong cluster, or "connection refused" on `localhost:8080` | You haven't run `source scripts/env.sh` in this shell. Check with `kubectl config current-context`. |
| `Forbidden … kubernetes-admin` right after a restart | API server is still starting. Wait for `kubectl get --raw /readyz`, or rerun `./scripts/create-cluster.sh`. |
| `.kube/payment-lab.kubeconfig` missing | `./scripts/create-cluster.sh` re-exports it from the running cluster. |
| Pods stuck `Pending` | `kubectl describe pod <name>` → Events. Often CPU or memory requests are too large for the 8 GiB VM. |
| `ImagePullBackOff` | `kubectl describe pod`. Check the image name and tag, and network access from the VM. |
| Node `NotReady` | `kubectl describe node <name>`, then `docker logs payment-lab-worker`, then `docker exec payment-lab-worker journalctl -u kubelet --no-pager \| tail`. |
| Everything slow | `docker stats`. Raise VM resources: `colima stop`, then `colima start --cpu 6 --memory 10`. |
| Create script says node count mismatch | You changed `kind/payment-lab.yaml`. Topology changes need a delete and recreate. |

## Verification (2026-10-17)

`scripts/verify.sh` passed, both right after creation and again after a Colima stop/start:

1. Context is `kind-payment-lab`, and the API server is reachable.
2. All 3 nodes are `Ready` (v1.37.0, containerd 2.3.4).
3. CoreDNS, kube-proxy, kindnet, etcd, and the control-plane Pods are all Ready.
4. A 2-replica nginx Deployment rolled out, one Pod on each worker.
5. A ClusterIP Service got EndpointSlices for both Pods.
6. A busybox client resolved `web.lab-verify.svc.cluster.local` through CoreDNS (10.96.0.10) and got `HTTP/1.1 200 OK`.
7. Helm 4.3.1 installed, listed, and uninstalled a local chart.

The `lab-verify` namespace is deleted afterwards.
