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

## Stage 3a - Charging API (`charging-api`)

**What:** build the application that handles credit requests, publish it as a container image to the lab's own registry, and deploy it in `billing-cde` next to the database.

**What it does:** a small HTTP service (Python standard library plus `pymongo`). `POST /reserve` sets aside credit with a single atomic debit that only matches when the balance is sufficient; `POST /commit` charges what was actually used and releases the rest; `POST /refund` releases everything; `GET /balance/<subscriber>` and `GET /healthz` complete the API. Every movement is written to the ledger. A unique sparse index on `finalSession` makes a second commit or refund of the same session fail (HTTP 409) instead of crediting twice.

**Design choices:**

- Self-contained image: dependencies are installed at build time (`pymongo==4.18.2`, pinned). Nothing is downloaded at runtime, which matters because the protected zone will have default-deny egress.
- Built with BuildKit and nerdctl on a worker node (no Docker daemon) and pushed to the lab's Harbor registry. The image is pulled by containerd on the node, not by the pod, so pod-level network policy does not affect the pull.
- Hardened workload: non-root (uid 10001), read-only root filesystem (with an `emptyDir` for `/tmp`), all Linux capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp profile, and no ServiceAccount token mounted (the application does not need the Kubernetes API).
- Database credentials come from the `charging-api-db-credentials` Secret through environment variables. Nothing sensitive is in git.
- Probes use `exec` instead of network checks, so they do not depend on network policy.

**Build and publish (worker node):**

    sudo systemctl start buildkit
    sudo nerdctl build -t harbor.lab.local/library/charging-api:0.1.0 .
    sudo nerdctl push harbor.lab.local/library/charging-api:0.1.0
    sudo systemctl stop buildkit

**Deploy:**

    kubectl apply -f manifests/20-charging-api.yaml
    kubectl -n billing-cde rollout status deployment/charging-api

**Verify (real output).** Functional tests, run from inside the pod, on subscriber `sub-0003` (starting balance 1500):

    reserve 100              -> 200, balance 1400
    reserve same session     -> 409 session already reserved
    commit, used 60          -> 200, released 40, balance 1440
    commit again             -> 409 session already finalized
    refund after commit      -> 409 session already finalized
    reserve 999999           -> 402 insufficient balance

Concurrency test: 10 simultaneous reservations of 100 on `sub-0001` (balance 500), released at the same instant with a barrier:

    status codes: 200 x5, 402 x5
    final balance: 0

Exactly five succeeded and five were rejected; the balance never went negative.

**Known limitations (stated on purpose):**

- Without a replica set MongoDB has no multi-document transactions. Closing a session records the event first and credits second, so a crash between the two would leave the released amount uncredited (it fails on the safe side). `reserve` debits before writing its ledger entry, so a crash between the two would leave a debit without a record. Reconciling balances against the ledger would catch both.
- The registry's image scan (Trivy) reports 162 findings, highest severity High, 6 with a fix available. The High findings reviewed so far are in base operating system packages with no fixed version published yet. A full triage is pending.
- These tests were run from inside the pod, and no network policy is applied yet, so nothing here demonstrates segmentation.

**Rollback:**

    kubectl delete -f manifests/20-charging-api.yaml

The image stays in the registry.
