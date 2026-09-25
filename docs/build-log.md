# Build log

Step-by-step record of how this project is built on a real cluster. Each stage is a small, reversible, verifiable change delivered as its own pull request. Nothing is marked as validated until it has real evidence.

## Stage 1 - Namespaces

**What:** create the three namespaces that model the trust zones: `billing-cde` (protected zone), `billing-clients` and `billing-admin`. Each carries a `zone` label (`cde`, `clients`, `admin`) that later network policies will select on.

**Why first:** everything else (database, API, clients, policies) lives inside these namespaces, and labels give policies a stable identity to match instead of IP addresses.

**Apply:**

    kubectl apply -f manifests/00-namespaces.yaml

**Verify:**

    kubectl get ns --show-labels | grep billing
    kubectl get networkpolicy -A --no-headers | awk '{print $1}' | sort | uniq -c

**Result:** the three namespaces are `Active` with their `zone` labels. Network policies exist only in the namespaces of other workloads on the cluster; there are none in `billing-*`, so the starting point for the "before" measurement is an unrestricted network.

**Rollback:**

    kubectl delete -f manifests/00-namespaces.yaml

Deleting a namespace removes everything inside it, so this fully undoes the stage.
