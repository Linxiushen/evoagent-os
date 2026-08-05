# Security

HarnessLab treats model output, tool arguments, and remote protocol documents as untrusted input.

- Read-only tools may be auto-approved.
- Tools with side effects fail closed unless an explicit approval provider is installed.
- API keys are read from environment variables and are never persisted in traces.
- Raw provider payloads are reduced before they enter the event log.

Please report vulnerabilities privately to the maintainer email listed on the GitHub profile. Do
not include live credentials, private repository contents, or production traces in an issue.

