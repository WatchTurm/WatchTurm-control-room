# Troubleshooting: Snapshot Data Gaps (TAP2, B2C Prod, Reminders)

This doc covers partial or missing data after restoring snapshot runs: TAP2 lower envs, B2C prod, and Reminders & Signals "no data".

---

## 1. TAP2.0 dev / alpha / beta / QA – no data

**Symptom:** TAP2 DEV, Alpha, Beta, QA show no deployment/build data (Argo Unknown, Build `-`, Last deploy `-`). UAT/PROD may have data.

**Data source:** Components come from **kustomization** in infra repos (`*-infra`), not from Argo. Snapshot fetches `kustomization.yaml` per service and env, then enriches with TeamCity.

**Causes and checks:**

- **Kustomization path**
  - Snapshot tries, in order: `envs/{env}/kustomization.yaml`, `envs/{env}/kustomization.yml`, then `overlays/{env}/kustomization.yaml`, `overlays/{env}/kustomization.yml`. Env keys are normalized to lowercase (e.g. `alpha`, `dev`).
  - If your TAP2 infra uses **only** `overlays/{env}`, the overlays fallback was added for this. Ensure snapshot is up to date.
  - If you use a different layout (e.g. `base/`, `envs/DEV` uppercase-only), we don’t support it yet. You’d need to align with `envs/{env}` or `overlays/{env}` (lowercase).

- **Logs**
  - Look for `[WARN] … – nie mogę pobrać kustomization` and `kustomization_fetch_failed` (project, env, service, infra_repo, error). These indicate fetch failures (404, auth, network).
  - Look for `kustomization_no_tags` (project, env, service, infra_repo): file found but no image tags/newTags extracted.

- **Config**
  - `configs/tap2.yaml`: check `environments` (dev, alpha, beta, qa, uat, prod) and `services` (each with `infraRepo`, `teamcityBuildTypeId`). A service with `envs: [dev, qa, uat, prod]` is **excluded** from alpha and beta.

**Actions:** Confirm infra layout (`envs/` vs `overlays/`), run snapshot, and inspect logs for `kustomization_fetch_failed` / `kustomization_no_tags` for the affected project/env/service.

---

## 2. B2C prod – only half of repos / components

**Symptom:** B2C prod shows fewer components than expected (e.g. half of services). Other envs (e.g. blue, QA) may look fine.

**Data source:** Same as above: kustomization per service per env, then TeamCity. There is no global limit on how many services we process; each service is handled individually.

**Causes and checks:**

- **Per-service failures**
  - When kustomization fetch fails we add a **placeholder** component (NO_KUSTOMIZATION) and continue. When we find kustomization but no tags, we add a placeholder (NO_TAG_FOUND) and continue. We do **not** drop services silently.
  - Check logs for `kustomization_fetch_failed` and `kustomization_no_tags` with `project=PO1_B2C`, `env=prod`, and the missing `service` / `infra_repo`. That will tell you which repos are failing and why.

- **GitHub**
  - Rate limits, 404s, or auth issues can cause fetch failures. Verify token scope and rate-limit headers in responses.

- **Config**
  - `configs/po1_b2c.yaml`: no per-service `envs` filter, so all services are expected in prod. If you add `envs` later, exclude prod only for some services, those will not appear in prod.

**Actions:** Run snapshot, grep logs for `kustomization_fetch_failed` and `kustomization_no_tags` with `project=PO1_B2C` and `env=prod`. Fix infra (paths, tags) or GitHub access for the reported repos.

---

## 3. Reminders & Signals – "No Datadog data" for P01_B2C (blue, green, …) but B2C view has data

**Symptom:** Reminders show "No data" / "No Datadog observability data" for P01_B2C and envs like blue, green, yellow, etc. When you open B2C, deployment data (repos, builds, branches) is present.

**Explanation:** These are **different data sources**:

- **Reminders "No data"** = **Datadog observability**: metrics (CPU, memory, pods, error rate, p95, etc.) for that project/env. "No data" means Datadog returned no series for the configured queries and tags (e.g. `env:blue`, `env:green`).
- **B2C platform view** = **Snapshot deployment data**: kustomization + TeamCity (tags, builds, branches, deployer). This comes from GitHub and TeamCity, not from Datadog.

So you can have **deployment data** (B2C) and **no Datadog observability data** (Reminders) at the same time. Both can be correct.

**Checks:**

- Datadog `envMap` in config (e.g. for PO1_B2C) maps config envs to Datadog tags. Ensure your Datadog metrics use the same tag names (e.g. `env:blue`, `env:green`) as in the queries.
- Reminders UI now use "No Datadog data" (or "No Datadog observability data") to make it clear this is about **Datadog metrics**, not deployment data.

**Actions:** If you intend to use Reminders for observability, configure Datadog tags and queries to match your envs. If you only care about deployment data, you can ignore these Reminders for those envs.

---

## Log reference

| Log event                   | Meaning                                                        |
|----------------------------|----------------------------------------------------------------|
| `kustomization_fetch_failed` | Could not fetch kustomization for project/env/service; check path, GitHub, token. |
| `kustomization_no_tags`    | Kustomization fetched but no image tags/newTags found.         |
| `config_skipped`            | Project skipped (e.g. missing `environments` or `services`).   |

These are emitted via the snapshot logger (e.g. structlog). Check your snapshot logs (scheduler, API server, or CLI) for these keys.
