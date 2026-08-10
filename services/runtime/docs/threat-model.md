# Threat Model

## Protected assets

Operator files, model credentials, channel secrets, private network services, conversation state, approval integrity and prompt provenance.

## Trust boundaries

- Channel payloads and model output are untrusted.
- Tools are code and receive only a scoped `ToolContext`.
- Workspace containment is a convenience boundary; OS/container sandboxing remains required for hostile code.
- Gateway clients are trusted only after bearer-token or webhook-HMAC verification.

## Implemented controls

| Threat | Control |
|---|---|
| Prompt-injected side effects | risk classification and approval before execution |
| Directory traversal | resolved-path containment under configured workspace |
| SSRF | HTTPS-only, explicit host allowlist, DNS checks against private/reserved addresses, no redirects |
| Replay ambiguity | unique run/approval IDs and append-only event records |
| Silent behavior drift | immutable prompt versions, comparative evaluation and manual promotion |
| Oversized file/tool output | 1 MB read/write/fetch limits |
| Webhook spoofing | per-channel HMAC-SHA256 secret |

## Residual risks

SQLite is not a distributed consensus system; DNS rebinding remains possible between validation and connection; tool code runs in the Gateway process; event payloads are not encrypted at rest. Production operators should use an OS identity with minimal permissions, a reverse proxy with TLS, secret management, outbound network policy and a Docker/VM sandbox for untrusted execution.

