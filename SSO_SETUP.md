# SSO / Authentication Setup

WatchTurm Control Room does **not** include built-in authentication. Deploy it behind a reverse proxy that handles SSO. Each company configures their own IdP (Azure AD, Okta, Keycloak, Google Workspace, etc.).

---

## Recommended: oauth2-proxy

[oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) sits in front of nginx and validates sessions before forwarding requests.

### 1. Architecture

```
[User] → [oauth2-proxy :4180] → [nginx :80] → [Control Room static + API]
              ↓
        [IdP: Azure AD / Okta / Keycloak / Google]
```

### 2. Install oauth2-proxy

```bash
# Example: Linux (binary from releases)
curl -L https://github.com/oauth2-proxy/oauth2-proxy/releases/download/v7.5.0/oauth2-proxy-v7.5.0.linux-amd64.tar.gz | tar xz
sudo mv oauth2-proxy-v7.5.0.linux-amd64/oauth2-proxy /usr/local/bin/
```

### 3. Configure for your IdP

**Google OAuth example** (simplest for testing):

```bash
export OAUTH2_PROXY_CLIENT_ID="your-google-client-id"
export OAUTH2_PROXY_CLIENT_SECRET="your-google-client-secret"
export OAUTH2_PROXY_COOKIE_SECRET="$(openssl rand -base64 32)"
export OAUTH2_PROXY_EMAIL_DOMAINS="*"   # or "your-company.com"
export OAUTH2_PROXY_UPSTREAMS="http://127.0.0.1:80"

oauth2-proxy --http-address="0.0.0.0:4180" \
  --provider=google \
  --upstream="http://127.0.0.1:80"
```

**Azure AD / Microsoft Entra ID** – use `--provider=azure` and set tenant, client ID, client secret. See [oauth2-proxy Azure docs](https://oauth2-proxy.github.io/oauth2-proxy/docs/configuration/oauth_provider#azure-auth-provider).

**Keycloak** – use `--provider=keycloak-oidc`. Configure realm and client.

### 4. Nginx in front of oauth2-proxy

```nginx
# External: only oauth2-proxy exposed
server {
    listen 80;
    server_name control-room.your-company.com;

    location / {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Scheme $scheme;
        proxy_set_header X-Auth-Request-Redirect $request_uri;
    }
    location /oauth2/ {
        proxy_pass http://127.0.0.1:4180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Scheme $scheme;
    }
}
```

Or use `auth_request` so nginx validates via oauth2-proxy – see [oauth2-proxy nginx config](https://oauth2-proxy.github.io/oauth2-proxy/docs/configuration/overview#auth-request).

---

## Alternative: nginx + auth_request

If you have an OIDC-compatible IdP, you can use nginx `auth_request` with a module or a small auth service. Each company integrates their IdP (Azure AD, Okta, Keycloak) – we do not ship IdP-specific configs.

---

## What we do not provide

- Preconfigured Azure AD / Okta / Keycloak manifests – tenant IDs, client IDs, and secrets are company-specific.
- Built-in login form – the app is static; auth is always handled by the reverse proxy.

---

## Summary

1. Deploy oauth2-proxy (or similar) in front of nginx.
2. Register an OAuth/OIDC application in your IdP.
3. Configure oauth2-proxy with client ID, secret, provider.
4. Point your domain at the proxy. Users authenticate via your IdP before reaching the dashboard.
