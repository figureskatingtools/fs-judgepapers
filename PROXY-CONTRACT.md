# Proxy contract

This repo is **backend-only**. The Function App deployed from `infra/` has no
login surface of its own: it is called exclusively, server-to-server, by the
router in the **figureskatingtools-site** repo, which owns the domain, the
Entra Easy Auth session and the static frontend at
`https://figureskatingtools.com/judgepapers/`.

This document is the contract between that router and this backend. Both sides
must change together.

## Request path

```
Browser
  └─ https://figureskatingtools.com/judgepapers/api/<route>      (session cookie, Easy Auth)
       │  router strips the /judgepapers prefix and re-issues the request
       ▼
     https://func-fs-judgepapers-<hash>.azurewebsites.net/api/<route>
       + x-proxy-secret: <PROXY_SHARED_SECRET>
       + x-forwarded-user-email: <signed-in user's email>
```

The Function App keeps the default `/api` route prefix — the router only removes
its own `/judgepapers` segment, so no backend route ever changes.

The Function App is **`AllowAnonymous`** at the platform level
(`infra/modules/function.bicep`, `authsettingsV2.globalValidation`). It must be:
the router forwards an email header, not a bearer token, so Easy Auth
enforcement would 401 every proxied request before the app's own header check
runs. CORS is empty (`allowedOrigins: []`) because no browser ever calls this
host directly.

## Headers

| Header | Direction | Meaning |
|---|---|---|
| `x-proxy-secret` | router → function | Proof the request came from the router. Compared against the `PROXY_SHARED_SECRET` app setting. |
| `x-forwarded-user-email` | router → function | The authenticated user's email, extracted by the router from the Easy Auth principal. |

Both header names are matched case-insensitively (each is looked up in both
`Title-Case` and lowercase form in `function_app.py`).

### `PROXY_SHARED_SECRET`

- Set on the Function App via the `proxySharedSecret` Bicep parameter (injected
  from the per-environment GitHub Environment secret `PROXY_SHARED_SECRET`).
- The router holds the same value per environment as
  `PROXY_SHARED_SECRET_JUDGEPAPERS`.
- `_proxy_secret_ok()` **fails open when the app setting is empty** — local dev
  and brief pre-rollout windows work without it. When it is set, a missing or
  mismatched `x-proxy-secret` makes `get_user_email_from_header()` return `None`,
  and every endpoint turns that into **401**.
- Rotate by updating both sides (this repo's env secret + the site repo's
  `PROXY_SHARED_SECRET_JUDGEPAPERS`) and redeploying; old and new hosts can run
  in parallel during a migration only while they share the same value.

### Identity precedence in `get_user_email_from_header()`

`infra/functions/function_app.py` resolves the caller's identity in this fixed
order; the first hit wins:

0. **`_proxy_secret_ok()` gate** — if it fails, return `None` immediately (401).
   Nothing below is consulted.
1. `X-MS-CLIENT-PRINCIPAL-NAME` — direct App Service Easy Auth header, present
   only if the Function App is ever fronted by Easy Auth itself.
2. **`X-Forwarded-User-Email`** — the normal production path (set by the router).
3. `X-MS-CLIENT-PRINCIPAL` — base64 JSON principal (legacy Static Web Apps
   shape); `userDetails` is used.
4. `Authorization: Bearer <jwt>` — payload decoded without signature
   verification; claims tried in order `preferred_username`, `email`, `upn`,
   `unique_name`, then the `emails[0]` array, then `name`/`oid`.

Steps 1, 3 and 4 are legacy/compatibility paths kept for direct-call debugging.
They are only reachable **after** the shared-secret gate, so they cannot be used
to spoof an identity from the public internet.

Every endpoint must call `get_user_email_from_header(req)` and return **401**
when it returns `None`. `is_user_allowed(email)` is the (currently
allow-everyone) authorization hook layered on top.

## Local testing

`func start` from `infra/functions/` serves the same routes on
`http://localhost:7071`. With no `PROXY_SHARED_SECRET` in
`local.settings.json`, the gate is off and only the email header is needed:

```bash
curl -s "http://localhost:7071/api/check_user_permission" \
  -H "x-forwarded-user-email: markus@example.com"
# {"allowed": true, "email": "markus@example.com"}

# No identity header at all → 401
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:7071/api/check_user_permission"
# 401
```

To exercise the gate exactly as production does, add
`"PROXY_SHARED_SECRET": "local-dev-secret"` to the `Values` block of
`infra/functions/local.settings.json`, restart `func start`, and send both
headers:

```bash
# Correct secret → 200
curl -s "http://localhost:7071/api/list_competitions" \
  -H "x-proxy-secret: local-dev-secret" \
  -H "x-forwarded-user-email: markus@example.com"

# Missing/wrong secret → 401, regardless of the email header
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:7071/api/list_competitions" \
  -H "x-proxy-secret: wrong" \
  -H "x-forwarded-user-email: markus@example.com"
# 401
```

The same two headers work against a deployed Function App
(`https://func-fs-judgepapers-<hash>.azurewebsites.net/api/...`) using that
environment's real secret — useful for isolating whether a failure is in the
router or in the backend.
