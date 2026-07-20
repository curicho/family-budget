# family-budget

Self-hosted UK household finance platform (see full design doc delivered separately).

- `app/` — Python application: FastAPI api, web (SPA + proxy), worker (cron jobs), migrate, backup
- `db/schema.sql` — Postgres schema (becomes migration 001 in the image)
- `helm/family-budget/` — Helm chart
- `docs/` — Mac mini setup guide, payslip parser spec, **HOW-DEPLOYMENT-WORKS.md** (read this to understand push → build → rollout)
- `Dockerfile`, `.github/workflows/build.yml` — multi-arch image build to GHCR

## Ship it
1. Push this repo to GitHub (public repo, or make the GHCR package public after first build).
2. Actions builds `ghcr.io/<user>/family-budget:latest` for arm64+amd64.
3. `helm upgrade family-budget ./helm/family-budget -n family-budget \
      --set image.repository=ghcr.io/<user>/family-budget \
      --set backup.agePublicKey=age1...`
   (repository must be all lowercase)
4. Open the app, click "First-run: create user", log in, add members/accounts.
