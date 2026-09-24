# Security

## Supported posture

This is a production-oriented foundation with tenant authentication and
authorization controls. It currently uses simulated commerce data and must not
receive real customer information until the remaining operational controls
below are addressed.

## Trust boundaries

- Customer messages, feedback, cookies, and model output are untrusted.
- Knowledge documents are trusted repository content today; future external
  retrieval sources must be treated as untrusted and tenant-scoped.
- Tool results are authoritative only for the resource the authenticated caller
  is authorized to access.
- The model may propose an action but never grants permission to perform it.

## Existing controls

- Allowlisted tools and bounded planner loops
- Code-enforced cancellation and return rules
- Separate-turn confirmation for destructive actions
- Card-like input redaction
- Output identifier provenance checks
- Request-size limit and non-disclosing HTTP errors
- `HttpOnly`/`SameSite` session cookies, with opt-in `Secure`
- Opaque, hashed, expiring, and revocable database-backed sessions
- Distinct customer/admin cookies with strict route realms and no cross-role
  authentication fallback
- Customer/admin role separation with server-controlled role assignment
- PostgreSQL row-level security and application-level ownership checks
- One-use, expiring password-reset tokens that revoke existing sessions
- Tenant ownership on orders, conversations, messages, memory, support
  requests, drafts, approvals, executions, and audit events
- QStash-signed workflow callbacks with current/next key rotation
- Payload-bound submission idempotency, leased processing claims, unique action
  execution keys, retry recovery, and durable dead-letter records
- Fail-closed approval expiry: no human response means no action executes
- Internal traces disabled by default
- Deterministic tests for tool, policy, authentication, and tenant boundaries

## Known release blockers

- No transactional adapter to a real retailer/commerce provider
- No distributed rate limiting or concurrency control across processes
- Password-reset delivery is not connected to an email provider
- No CSRF token mechanism beyond `SameSite=Lax` cookies; reassess before
  introducing cross-site browser integrations
- Demo credentials must be replaced or disabled outside demonstration
  environments
- Regex injection detection is defense-in-depth, not a security boundary
- Deployment still requires managed secret rotation, TLS termination, backups,
  recovery drills, and security monitoring

## Reporting

Do not open a public issue containing credentials or customer data. Report a
suspected vulnerability privately to the repository owner with reproduction
steps, affected commit, and impact.
