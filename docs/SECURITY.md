# Security Policy

EvoAgent OS v0.1 is a development preview. It provides security-oriented building blocks, not a certified secure deployment. Do not expose the reference stack to an untrusted network without the controls below.

## Supported versions

Only the latest commit on `main` is currently supported. No stable release branch or backport policy exists during v0.1.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Linxiushen/evoagent-os/security/advisories/new). Do not open a public issue for credential exposure, authorization bypass, remote code execution, sandbox escape, consent bypass or private-data disclosure.

Include:

- Affected component and commit SHA
- Preconditions and minimal reproduction
- Observed and expected behavior
- Security impact and data at risk
- Suggested mitigation, if known

Do not include real credentials, personal biometric media or other people's private data. Maintainers will acknowledge a complete report when available, coordinate validation and remediation, and publish credit only with the reporter's consent. v0.1 does not promise a fixed response SLA.

## Minimum deployment controls

1. Keep service ports on loopback or a private service network.
2. Terminate TLS at a maintained reverse proxy and reject clear-text public traffic.
3. Require identity-aware authentication and authorization in front of every service. Set a strong `EVOAGENT_GATEWAY_TOKEN`; do not rely on an empty Runtime token.
4. Store model keys, signing keys, webhook secrets and persona signing material in a secret manager. Never put them in Git, image layers, prompts, logs or issue reports.
5. Run as a non-root identity with read-only filesystems where possible, minimal volume mounts, dropped Linux capabilities and outbound allowlists.
6. Isolate untrusted tool and Skill execution in disposable containers or VMs without ambient credentials.
7. Give workers short-lived workload identity. The reference Fleet registration endpoint is not an authentication system.
8. Back up SQLite state and artifact volumes consistently, encrypt backups, restrict restore access and test restoration.
9. Export security-relevant events to access-controlled, append-only storage.
10. Establish rate limits, quotas and trusted usage metering before serving multiple users.

## Secret handling

- Generate independent random values for Runtime and EchoWeave access tokens.
- Use separate keys for development, CI, staging and production.
- Keep Forge private keys outside the repository and registry volume. Distribute public-key fingerprints through a separately trusted channel.
- Treat any secret pasted into chat, a trace, build output or Git history as compromised; revoke and replace it.
- Do not put secrets in `examples/` or `.env.example`. The included values are placeholders only.

## Skill and tool safety

Static scanning, a passing test suite and a valid signature are independent signals. None of them proves a Skill is benign. Before installation:

- Pin the artifact SHA-256 and expected signing-key fingerprint.
- Review declared capabilities and dependency changes.
- Reject undeclared network, filesystem, process or credential access.
- Execute with an allowlisted workspace and egress policy.
- Require human approval for irreversible external effects.

## Realtime identity and media

The software must not be used for undisclosed impersonation. A real-person persona requires verified authorization covering the intended voice, face, purpose, processors and duration. Consent must be revocable, and generated output must remain visibly identified as AI/synthetic media. The demo persona is fictional and is the only appropriate default for public demonstrations.

## Security references

- [Threat model](THREAT_MODEL.md)
- [Architecture and trust boundaries](ARCHITECTURE.md)
- [Operations and incident response](OPERATIONS.md)
- Component-specific policies under each `services/*` directory
