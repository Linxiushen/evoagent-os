# Security

Please report vulnerabilities through GitHub private vulnerability reporting instead of a public
issue.

## Trust model

HarnessLab treats model output, tool arguments, tool results, MCP responses, and remote capability
documents as untrusted data.

- Side-effecting tools fail closed without an explicit approval provider.
- Common credential keys and inline bearer tokens are redacted before trace event publication.
- Raw model context is not serialized through run APIs.
- API keys are read from environment variables and are never intentionally added to trace events.
- MCP tools default to side-effecting when remote annotations are incomplete.
- Capability discovery does not execute provider-supplied code.

Redaction is defense in depth. Do not place secrets in prompts, deterministic fixtures, or issue
attachments. Review exported artifacts before sharing them outside your trust boundary.
