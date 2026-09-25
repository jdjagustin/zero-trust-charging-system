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

## Stage 2 - Database (`balance-db`)

**What:** deploy the protected asset: a MongoDB instance in `billing-cde` holding synthetic balances and a transaction ledger, with authentication on and two least-privilege application users.

**Design choices:**

- MongoDB `4.4.29`: the lab's older CPUs lack AVX, which MongoDB 5.0+ requires. The data design (atomic single-document updates, read-only role) works the same on 4.4.
- Small footprint: 256 MB WiredTiger cache cap, memory request 256Mi / limit 512Mi, 1Gi volume on the default `local-path` StorageClass.
- Probes use `exec` instead of `tcpSocket`/`httpGet`: network probes are issued by the kubelet from the node and a default-deny policy would block them; `exec` probes run inside the container and do not depend on network policy.
- Credentials never live in git. The root password and both application passwords are random, created imperatively as Kubernetes Secrets, and passed to MongoDB over stdin so they never appear in process arguments.

**Apply:**

    kubectl -n billing-cde create secret generic balance-db-credentials \
      --from-literal=MONGO_INITDB_ROOT_USERNAME=admin \
      --from-literal=MONGO_INITDB_ROOT_PASSWORD="$(openssl rand -base64 24 | tr -d '=+/')"
    kubectl apply -f manifests/10-balance-db.yaml
    kubectl -n billing-cde rollout status deployment/balance-db --timeout=240s

Seed synthetic data (10 subscribers, integer balances to avoid rounding errors):

    kubectl -n billing-cde exec -i deploy/balance-db -- sh -c \
      'mongo --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin' \
      < scripts/seed-balances.js

Two application users, created in database `billing`: `charging_api` (role `readWrite`, Secret `charging-api-db-credentials` in `billing-cde`) and `reporting_ro` (role `read`, Secret `reporting-db-credentials` in `billing-clients`).

**Verify (real output):**

- `db.version()` returns `4.4.29`.
- `listDatabases` without credentials returns `ok: 0` (authentication is enforced); with the root credentials it returns `ok: 1`.
- `reporting_ro`: reads 10 documents; a write is rejected with `not authorized on billing to execute command`.
- `charging_api`: a write matches 1 document; querying users in the `admin` database returns `ok: 0`.
- Resource use at rest: about 23m CPU and 135Mi memory.

**Rollback:**

    kubectl delete -f manifests/10-balance-db.yaml
    kubectl -n billing-cde delete secret balance-db-credentials charging-api-db-credentials
    kubectl -n billing-clients delete secret reporting-db-credentials

The volume uses reclaim policy `Delete`, so removing it also removes the (synthetic) data.
