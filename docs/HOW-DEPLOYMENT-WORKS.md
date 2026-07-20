# How your build & deployment works

*A plain-English guide to what happens between "I changed a file" and "the app is updated" — written for this project, using the exact commands and failures you've already lived through.*

---

## The big picture

Your code takes a journey through four places. Each place has one job:

```
   YOUR LAPTOP              GITHUB                 GHCR                YOUR CLUSTER
  (where you edit)     (where code lives)    (image warehouse)      (where it runs)

  ┌─────────────┐      ┌─────────────┐      ┌─────────────┐      ┌───────────────┐
  │  edit files │      │  git repo   │      │  "latest"   │      │  pods running │
  │             │─────►│             │─────►│   image     │─────►│  your app     │
  │  git push   │      │  Actions    │      │             │      │               │
  └─────────────┘      │  builds it  │      └─────────────┘      └───────────────┘
                       └─────────────┘
       step 1              step 2               step 3               step 4
      (manual)           (automatic)          (automatic)           (manual)
```

Two of these steps are automatic, two are yours. The golden rule of the whole
system — the one that bit you twice — is:

> **push → wait for Actions to go green → THEN restart pods. Always in that order.**

Now let's walk each step.

---

## Step 1 — You push code

```bash
git add -A
git commit -m "describe the change"
git push
```

`git push` uploads your commits to GitHub. That's *all* it does. Nothing is
built, nothing is deployed, the app doesn't change. Think of it as posting a
recipe to a noticeboard — nobody has cooked anything yet.

---

## Step 2 — GitHub Actions builds an image (automatic)

Inside your repo there's a file: `.github/workflows/build.yml`. It tells
GitHub: *"whenever someone pushes to the `main` branch, run this job."* That's
the entire trigger mechanism — GitHub watches the branch, sees your push, and
starts a fresh cloud machine to do the work:

```
  your push arrives at GitHub
            │
            ▼
  ┌───────────────────────────────────────────────┐
  │  GitHub Actions runner (a temporary Linux VM) │
  │                                               │
  │  1. checkout   — downloads your repo          │
  │  2. buildx     — follows the Dockerfile:      │
  │       • start from python:3.12-slim           │
  │       • apt-get install postgresql-client,    │
  │         age, curl, ca-certificates  ◄─── remember this one?
  │       • pip install your app                  │
  │       • copy in db/schema.sql + migrations    │
  │  3. does it TWICE: once for amd64 (cloud      │
  │     machines) and once for arm64 (your Mac)   │
  │  4. push       — uploads the finished image   │
  │                  to ghcr.io                   │
  └───────────────────────────────────────────────┘
            │
            ▼
     ✅ green tick   (≈ 3–4 minutes)
```

**What's an "image"?** A frozen, complete copy of everything the app needs to
run: the operating system files, Python, every library, your code, the
migrations. Like a ready-meal — cooked once here, reheated identically
anywhere. This is why "it works on my machine" problems disappear: the cluster
runs the *exact* bytes that were built here.

The finished image is uploaded to **GHCR** (GitHub Container Registry) under
two names:

- `ghcr.io/curicho/family-budget:latest` — a *moving* label, always re-stuck
  onto the newest build
- `ghcr.io/curicho/family-budget:<commit-sha>` — a permanent label for that
  exact build

**The waiting matters.** Until the tick is green, `latest` still points at the
*previous* build. This is exactly what happened when your Banking section
didn't appear: you restarted pods at minute 1 of a 4-minute build, the pods
pulled the old image, and everything looked "deployed" while running old code.

---

## Step 3 — The image sits in the warehouse (automatic, instant)

GHCR just stores images and hands them out on request, like Docker Hub. One
gotcha you also hit: packages default to **private** even on public repos —
your cluster got `denied` until you flipped the package to public. That's a
one-time setting; it stays flipped.

---

## Step 4 — You roll out (manual)

Here's the key mental shift: **the cluster never watches GitHub.** It has no
idea you pushed. Pods keep running whatever image they started with — forever —
until something makes them restart. That something is you:

```bash
kubectl -n family-budget rollout restart deploy/family-budget-api deploy/family-budget-worker deploy/family-budget-web
```

What "rollout restart" actually does — and this is the elegant part — is **not**
"turn it off and on again". It's a careful handover:

```
   BEFORE                    DURING                       AFTER

  ┌──────────┐          ┌──────────┐  ┌──────────┐         ┌──────────┐
  │ old pod  │          │ old pod  │  │ new pod  │         │ new pod  │
  │ (v1)     │   ───►   │ (v1)     │  │ (v2)     │  ───►   │ (v2)     │
  │ serving  │          │ STILL    │  │ starting │         │ serving  │
  │ traffic  │          │ serving  │  │ up...    │         │ traffic  │
  └──────────┘          └──────────┘  └──────────┘         └──────────┘
                                            │
                              pulls the image, runs init
                              (migrations), passes health
                              check... only THEN does the
                              old pod get terminated
```

You watched this safety net work during the migration-checksum crash: the new
api pod kept failing its init, so Kubernetes **refused to kill the old one** —
your app stayed up for 40 minutes of debugging, served entirely by a pod
running the previous version. A broken deploy never takes the site down; it
just fails to replace it.

Per-pod sequence when the new pod starts:

1. **Pull** the image. Because we set `imagePullPolicy: Always`, the pod
   re-downloads `latest` every time — without that setting, Kubernetes says
   "I already have something called latest" and reuses the stale cached copy.
   (That was your *other* mystery: api and web pods running two different
   builds of "latest". Same name, different bytes underneath.)
2. **Init container** runs `app migrate` (api pod only): applies any new SQL
   files in `/app/migrations` exactly once, recording a checksum of each so an
   already-applied file that changes gets refused rather than guessed at.
3. **Main container** starts (`app serve` / `app worker` / `app web`).
4. **Readiness probe**: Kubernetes polls `/healthz` until the pod answers.
5. Traffic switches to the new pod; the old one is terminated.

---

## The whole journey on one page

```
 you                 GitHub               GHCR                 cluster
  │                    │                    │                     │
  │  git push          │                    │                     │
  ├───────────────────►│                    │                     │
  │                    │  Actions builds    │                     │
  │                    │  (3–4 min)         │                     │
  │                    ├───────────────────►│                     │
  │                    │   image stored     │                     │
  │   ✅ green tick    │   as :latest       │                     │
  │◄───────────────────┤                    │                     │
  │                    │                    │                     │
  │  kubectl rollout restart               │                     │
  ├────────────────────────────────────────┼────────────────────►│
  │                    │                    │   new pods pull     │
  │                    │                    │◄────────────────────┤
  │                    │                    │   image :latest     │
  │                    │                    ├────────────────────►│
  │                    │                    │     init: migrate   │
  │                    │                    │     healthz ✓       │
  │                    │                    │     traffic swaps   │
  │                    │                    │     old pods die    │
  │  hard-refresh browser (Cmd+Shift+R)                          │
  │  ───── because your browser caches the old page too ─────    │
```

---

## Cheat sheet

Every deploy, in full:

```bash
git add -A && git commit -m "what changed" && git push
# open the repo's Actions tab → wait for the green tick
kubectl -n family-budget rollout restart deploy/family-budget-api deploy/family-budget-worker deploy/family-budget-web
kubectl -n family-budget get pods        # wait for 1/1 Running on fresh pods
# hard-refresh the browser: Cmd+Shift+R
```

Checking what's actually deployed (the question behind most "nothing changed"
moments):

```bash
# which exact build is each pod running? (digests should MATCH across pods)
kubectl -n family-budget get pods -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.containerStatuses[0].imageID}{"\n"}{end}'

# does the running server have my new endpoint? (bypasses browser cache)
curl -s -o /dev/null -w "%{http_code}\n" https://solomons-laptop.tail52ab55.ts.net/api/import/csv
# 405/401 = endpoint exists (new code live) · 404 = still old code
```

Reading a broken pod:

```bash
kubectl -n family-budget get pods                          # what state is it in?
kubectl -n family-budget describe pod <name> | tail -15    # WHY (events at the bottom)
kubectl -n family-budget logs <name>                       # app's own output
kubectl -n family-budget logs <name> -c migrate            # init container (migrations)
```

Decoder ring for statuses you've personally met:

| Status               | Meaning                                | Your instance of it            |
|----------------------|----------------------------------------|--------------------------------|
| `InvalidImageName`   | image name malformed (uppercase!)      | placeholder `YOURUSER`         |
| `ErrImagePull` / `ImagePullBackOff` | image missing or private | GHCR package was private       |
| `Init:Error` / `Init:CrashLoopBackOff` | migrations failing   | edited an applied migration    |
| `ContainerCreating`  | pulling image / binding storage — wait | postgres first boot            |
| `1/1 Running`        | alive and passing health checks        | the goal                       |

## Rules learned the expensive way

1. **Never restart before the tick is green** — you'll deploy the previous build and swear nothing changed.
2. **`latest` is a label, not a version** — two pods can both say `latest` and run different code; compare digests when confused.
3. **Never edit an applied migration file** — even a comment changes its checksum; add a new numbered file instead.
4. **The browser caches too** — the last mile of every deploy is Cmd+Shift+R.
5. **Secrets are separate from deploys** — patching a secret does nothing to running pods; they read secrets at startup, so patch *then* restart.
