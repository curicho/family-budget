# Mac mini setup guide

Goal: a k3s-style Kubernetes cluster running 24/7 on your Mac mini, ready to `helm install` the family-budget chart, with the app reachable from your other devices over Tailscale (no open ports).

Works on Apple Silicon or Intel. Total time: ~45 minutes.

---

## 1. Prepare macOS for always-on duty

```bash
# Prevent sleep while plugged in (keeps the cluster alive)
sudo pmset -c sleep 0
sudo pmset -c disksleep 0

# Auto-restart after a power cut
sudo pmset -c autorestart 1
```

In **System Settings → General → Login Items**, you'll add OrbStack after step 2 so the cluster survives macOS reboots (updates will occasionally restart the machine — plan for it, don't fight it).

Also enable **System Settings → General → Sharing → Remote Login (SSH)** so you can administer the mini from your laptop.

## 2. Install the toolchain

Install Homebrew if you don't have it, then:

```bash
brew install orbstack kubectl helm k9s age
```

Why OrbStack: it's a lightweight Linux VM manager for macOS with a built-in single-node Kubernetes cluster. It's dramatically simpler than Lima/UTM + k3s by hand, idles at near-zero CPU, and starts automatically at login. `k9s` is an optional but excellent terminal UI for watching the cluster. `age` is for encrypting backups.

Open OrbStack once, then in **OrbStack → Settings**:
- **System**: allocate 4–6 GB memory, 2–4 CPUs (it only uses what's needed).
- **Kubernetes**: toggle **Enable Kubernetes**. Wait ~1 minute.
- **General**: enable **Start at login**.

Verify:

```bash
kubectl config use-context orbstack
kubectl get nodes
# NAME       STATUS   ROLES                  AGE   VERSION
# orbstack   Ready    control-plane,master   1m    v1.xx
```

OrbStack routes cluster traffic to macOS automatically — Services and Ingresses are reachable from the mini itself without extra port-forwarding.

## 3. Tailscale (private access, zero open ports)

```bash
brew install tailscale
sudo tailscaled install-system-daemon
tailscale up
```

Log in, then install Tailscale on your phone/laptop from the same account. Your mini now has a stable private IP (100.x.x.x) and a MagicDNS name like `mac-mini.tailnet-name.ts.net`, reachable from your devices anywhere — with nothing exposed to the public internet. This is the app's only access path; do not port-forward on your router.

Optional polish: `tailscale serve` can terminate HTTPS with a real certificate for your tailnet domain, giving you a green padlock without cert-manager/Let's Encrypt complexity:

```bash
tailscale serve --bg https / http://localhost:8080
```

(Point it at whatever NodePort/localhost port the ingress or web Service exposes — see chart notes.)

## 4. Cluster prerequisites

```bash
# Namespace
kubectl create namespace family-budget

# Ingress controller (skip if you use `tailscale serve` straight to the web Service)
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace
```

## 5. Secrets

Create one Secret with all provider credentials before installing the chart. Never commit this file; keep it in a password manager.

```bash
kubectl -n family-budget create secret generic family-budget-secrets \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=MINIO_ROOT_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=APP_SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=FIELD_ENCRYPTION_KEY="$(openssl rand -base64 32)" \
  --from-literal=GOCARDLESS_SECRET_ID="..." \
  --from-literal=GOCARDLESS_SECRET_KEY="..." \
  --from-literal=TRADING212_API_KEY="..." \
  --from-literal=COINBASE_API_KEY="..." \
  --from-literal=COINBASE_API_SECRET="..." \
  --from-literal=ANTHROPIC_API_KEY="..." \
  --from-literal=B2_KEY_ID="..." \
  --from-literal=B2_APP_KEY="..."
```

Where the keys come from: GoCardless Bank Account Data → free account at bankaccountdata.gocardless.com; Trading212 → app Settings → API (read-only key); Coinbase → coinbase.com/settings/api with **read-only** scopes; Anthropic → console.anthropic.com; Backblaze B2 → create a bucket + app key scoped to that bucket only.

Generate a backup encryption keypair and store the private half OFF the mini (password manager + printed copy):

```bash
age-keygen -o backup-key.txt   # move this file off the machine after noting the public key
```

## 6. Install the chart

```bash
cd family-budget
helm install family-budget ./helm/family-budget \
  -n family-budget \
  -f helm/family-budget/values.yaml \
  --set backup.agePublicKey="age1..."   # public key from step 5
```

Watch it come up with `k9s -n family-budget` or:

```bash
kubectl -n family-budget get pods -w
```

First boot: the api pod runs DB migrations (applies `db/schema.sql` via the migration tool) before serving; MinIO creates the `documents` and `backups` buckets via the init job.

## 7. Verify + first backup test

```bash
# App reachable?
curl -s http://localhost:30080/healthz     # or your tailscale serve URL

# Trigger a backup manually and confirm it lands in B2
kubectl -n family-budget create job --from=cronjob/family-budget-backup backup-test
kubectl -n family-budget logs job/backup-test -f
```

Then do the restore drill ONCE now, while there's nothing to lose: download the dump from B2, `age -d` it, restore into a scratch database. Ten minutes now buys you certainty later.

## 8. Ongoing care

macOS updates: let them install; OrbStack + the cluster auto-start on login (enable auto-login for your admin user in System Settings if the mini is physically secure, otherwise the cluster waits for you to log in after a reboot). Check the home page's "connection health" card weekly — it surfaces expiring GoCardless consents (90-day renewals) and stale pension valuations. Everything else is automated.
