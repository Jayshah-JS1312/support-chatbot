# Security

## Supported posture

This is a production-oriented foundation, not an authorization-complete support
system. It currently uses simulated commerce data and must not receive real
customer information.

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
- Internal traces disabled by default
- Deterministic tests for tool and policy boundaries

## Known release blockers

- No customer authentication or per-order authorization
- No durable idempotency or transactional commerce adapter
- Plaintext local session/customer persistence
- No distributed rate limiting or concurrency control across processes
- Regex injection detection is defense-in-depth, not a security boundary
- The built-in HTTP server is not a production application server

## Reporting

Do not open a public issue containing credentials or customer data. Report a
suspected vulnerability privately to the repository owner with reproduction
steps, affected commit, and impact.
