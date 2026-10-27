#!/bin/bash
set -euo pipefail

source "$(dirname "$0")/env.sh"

NAMESPACE="lab01"
# pods behind the acme-pay Service
MOCK_SELECTOR="app.kubernetes.io/name=processor-mock"

# record the verdict and every version the run tested against, so the result can be reproduced
write_summary() {
    local exit_code=$1
    local kc=(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE")
    local failed succeeded git_sha check_image mock_pods configmaps cm cm_hash

    failed=$("${kc[@]}" get "$job" -o jsonpath='{.status.failed}' || true)
    succeeded=$("${kc[@]}" get "$job" -o jsonpath='{.status.succeeded}' || true)
    failed=${failed:-0}
    succeeded=${succeeded:-0}

    # mark the SHA dirty when the working tree has uncommitted changes
    git_sha=$(git -C "$LAB_ROOT" rev-parse HEAD 2>/dev/null || echo "unknown")
    if [[ -n "$(git -C "$LAB_ROOT" status --porcelain 2>/dev/null)" ]]; then
        git_sha="$git_sha-dirty"
    fi

    check_image=$("${kc[@]}" get "$job" -o jsonpath='{.spec.template.spec.containers[0].image}' || true)

    # image digest and ConfigMaps of every pod the Service could have routed to
    mock_pods=$("${kc[@]}" get pods -l "$MOCK_SELECTOR" -o jsonpath='{range .items[*]}{"  "}{.metadata.name}{"  image="}{.status.containerStatuses[0].imageID}{"  configmaps="}{.spec.volumes[*].configMap.name}{"\n"}{end}' || true)
    configmaps=$("${kc[@]}" get pods -l "$MOCK_SELECTOR" -o jsonpath='{.items[*].spec.volumes[*].configMap.name}' || true)

    {
        echo "run_id:       $run_id"
        echo "verdict:      ${verdict:-ERROR}"
        echo "reason:       ${reason:-script exited with code $exit_code}"
        echo "attempts:     $((failed + succeeded)) ($failed failed, $succeeded succeeded)"
        echo "exit_code:    $exit_code"
        echo "finished_at:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "git_sha:      $git_sha"
        echo "check_image:  $check_image"
        echo "mock_pods:"
        echo "${mock_pods:-  none found}"
        echo "configmaps:"
        # names alone are not enough: processor-mock-responses is mutable, so hash its content
        for cm in $(echo "$configmaps" | tr ' ' '\n' | sort -u); do
            cm_hash=$("${kc[@]}" get configmap "$cm" -o jsonpath='{.data}' | shasum -a 256 | cut -c1-16 || true)
            echo "  $cm  sha256=$cm_hash"
        done
    } > "$run_folder/summary.txt"
}

# save final job state and logs from all attempts to the run folder
collect_results() {
    local exit_code=$1

    kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" \
        get "$job" -o yaml > "$run_folder/job.yaml" \
        || echo "Could not save job yaml"
    kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" \
        logs -l job-name="$run_id" --all-containers --tail=-1 --prefix > "$run_folder/pod-logs.txt" \
        || echo "Could not save pod logs"
    write_summary "$exit_code" || echo "Could not write summary"

    # show logs on screen as well when the check did not pass
    if [[ $exit_code -ne 0 ]]; then
        echo "Logs from all attempts of the job:"
        cat "$run_folder/pod-logs.txt"
    fi
    echo "--- summary ---"
    cat "$run_folder/summary.txt" || true
    echo "Results saved to $run_folder"
}

cleanup() {
    local exit_code=$?
    if [[ -n "${run_folder:-}" ]]; then
        collect_results "$exit_code"
    fi
    if [[ -n "${job:-}" ]]; then
        echo "Deleting $job..."
        kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" \
            delete "$job" --ignore-not-found || true
    fi
}
trap cleanup EXIT

# This script runs the unique contract check job in the lab01 namespace and waits for it to complete or fail.
# it store all results in $LAB_ROOT/results/<run-id>/:
run_contract_check() {
    # don't test a half-ready environment: a failure here is an environment problem, not a contract failure
    echo "Checking that deploy/processor-mock is fully rolled out..."
    if ! kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" \
            rollout status deploy/processor-mock --timeout=60s; then
        echo "FAIL: environment not ready, no contract check was run."
        exit 1
    fi
    # rollout status passes for a Deployment scaled to 0 ("0 of 0 updated"), so also
    # require at least one available replica, i.e. something can actually serve.
    available=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" \
        get deploy/processor-mock -o jsonpath='{.status.availableReplicas}')
    if [[ "${available:-0}" -lt 1 ]]; then
        echo "FAIL: environment not ready (0 available replicas), no contract check was run."
        exit 1
    fi

    echo "Running contract check job in namespace 'lab01'..."
    if ! job=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" create -f "$LAB_ROOT/labs/01-foundations/jobs/contract-check.yaml" -o name); then
        echo "Failed to create contract check job."
        exit 1
    fi

    echo "Successfully created job: $job"

    # create run folder
    run_id=$(basename "$job")
    run_folder="$LAB_ROOT/results/$run_id"
    mkdir -p "$run_folder"

    # save evetyhing to run folder
    exec > >(tee -a "$run_folder/run.log") 2>&1

    echo "Waiting for job to complete..."

    # poll the job status every 2 secs until it completes or fails or times out
    timeout=60
    elapsed=0
    while true; do
        status=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
        if [[ "$status" == "True" ]]; then
            echo "Contract check job completed successfully."

            # flag pass after retry if more than 1 attempt was made
            failed_attempts=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.failed}')
            succeeded_attempts=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.succeeded}')
            # treat empty as 0
            failed_attempts=${failed_attempts:-0}
            succeeded_attempts=${succeeded_attempts:-0}
            attempts=$((failed_attempts + succeeded_attempts))
            verdict="PASS"
            reason="Complete"
            if [[ $attempts -gt 1 ]]; then
                echo "Contract check job passed after $attempts attempts."
                reason="Complete after $attempts attempts"
            fi
            break
        fi

        # check if the job has failed and print reason as well with attempt count
        status=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}')
        if [[ "$status" == "True" ]]; then
            reason=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].reason}')
            attempts=$(kubectl --kubeconfig="$LAB_KUBECONFIG" --context="$KUBE_CONTEXT" -n "$NAMESPACE" get "$job" -o jsonpath='{.status.failed}')
            echo "Contract check job failed with reason: $reason"
            echo "Number of failed attempts: $attempts"
            verdict="FAIL"
            exit 1
        fi

        sleep 2
        elapsed=$((elapsed + 2))
        if [[ $elapsed -ge $timeout ]]; then
            echo "Contract check job timed out."
            verdict="TIMEOUT"
            reason="no Complete or Failed condition after ${timeout}s"
            exit 1
        fi
    done
}

run_contract_check
    