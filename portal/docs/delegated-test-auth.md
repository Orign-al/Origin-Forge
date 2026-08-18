# Controlled delegated test sessions

Portal-5A automated acceptance uses a separate, short-lived bearer credential
when an ordinary user's password/session is intentionally unavailable. The
credential is not a Portal cookie and never contains or replaces the target
user's password.

The local administrator command `create-delegated-test-session` is the only
creation path. It requires exactly one active `platform_owner` in the Portal
database, an active ordinary managed target, one or more scopes from the fixed
acceptance allowlist, and a 600–900 second TTL. The raw credential is printed
once for the acceptance runner; only its SHA-256 digest is persisted.

Every request resolves RBAC and `/self` ownership against the effective user.
The actor remains the platform owner. Creation and request audit records carry
both logins, the delegation ID, scopes, target, result, and expiry. Ordinary
cookie-only auth routes do not accept a delegated bearer. Delegated credentials
cannot reach password, SSH-key, lease, activation, recycle, admin, or terminal
operations, and have no renewal path.

Example controlled invocation (capture stdout without echoing it):

```text
h100-portal-admin create-delegated-test-session \
  --target origin-pilot \
  --scope self.jobs.submit \
  --scope self.jobs.read \
  --scope self.jobs.logs.read \
  --scope self.jobs.cancel \
  --scope self.container.read \
  --ttl-seconds 900
```

The credential must be supplied only as `Authorization: Bearer <credential>`
to the formal `/api/v1/self/*` API path and discarded after expiry. No manual
reconciliation or user lifecycle operation is part of this flow.
