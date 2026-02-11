# WatchTurm Control Room

**Open-source release management dashboard** – single pane of glass for deployment visibility across GitHub, TeamCity, and Jira. Built for DevOps and release managers.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## What it does

WatchTurm Control Room aggregates deployment data from your CI/CD pipeline and displays it in one dashboard:

- **Pipeline radar** – DEV / QA / UAT / PROD status per project
- **Release history** – tag changes, calendar view, deployer attribution
- **Ticket Tracker** – Jira tickets with PR evidence and deployment timeline (search by ticket key)
- **Runbooks** – Scope checker, Drift checker, Release diff (GitHub branch comparison)
- **Statistics** – deployment frequency, lead time

No agents, no database – just a snapshot generator (Python) and static frontend. Data from GitHub, TeamCity, Jira APIs.

---

## Tech stack

- **Frontend:** Static HTML/CSS/JS (no build step)
- **Backend:** Python 3.10+ (snapshot generator, Flask API server)
- **Integrations:** GitHub (required), TeamCity (builds), Jira (optional, Ticket Tracker)

---

## Quick start

1. Copy `.env.example` to `.env` and add your tokens.
2. Copy `MVP1/snapshot/configs/example.yaml` to `my-project.yaml` and edit (GitHub org, repos, TeamCity build IDs).
3. Run snapshot: `python MVP1/snapshot/snapshot.py`
4. Serve `web/` and `data/` from the same origin (nginx or `python web/start_local_server.py`).
5. Open `web/index.html` in a browser.

**Production:** Run `snapshot_api_server.py` as a systemd/PM2 service – it runs snapshot every 15–30 min automatically. See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md).

**SSO / auth:** Protect the dashboard with your company IdP (Azure AD, Okta, Keycloak). See [SSO_SETUP.md](SSO_SETUP.md).

---

## Deployment model

Snapshot runs **on a server** (cron, systemd, or built-in scheduler). Employees open the dashboard in a browser – no Python on their machines.

---

## Before pushing to a public repo

Run `git status` and verify that `data/latest.json`, `data/release_history/`, `data/history/`, and any `configs/*.yaml` except `example.yaml` are **not** staged.

---

## Suggested GitHub repository settings

For better discoverability, set these in your repo’s **About** section:

- **Description:** `Open-source release management dashboard for DevOps. GitHub, TeamCity, Jira integrations. Pipeline radar, release history, ticket tracker.`
- **Topics:** `release-management`, `devops`, `github`, `teamcity`, `jira`, `deployment-dashboard`, `cicd`, `release-visibility`, `python`, `dashboard`

---

## License & credits

**Apache License 2.0.** See [LICENSE](LICENSE).

Developed by **Mateusz Zadrożny** within the WatchTurm initiative.  
[WatchTurm on LinkedIn](https://www.linkedin.com/company/watchturm)
