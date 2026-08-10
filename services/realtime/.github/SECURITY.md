# Security Policy

## Supported versions

Security fixes currently target the `0.2.x` development line on the default
branch. This project is a research preview; do not expose it directly
to the public internet without the controls in `docs/DEPLOYMENT.md`.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Linxiushen/echoweave-rtc/security/advisories/new).
Do not open a public issue for vulnerabilities, leaked credentials, consent
records, replay artifacts, or paths to private persona media.

Include the affected revision, impact, a minimal reproduction using synthetic
data, and any known mitigation. Never attach real-person audio/video, biometric
data, API keys, or production logs. The maintainer aims to acknowledge reports
within three business days and provide an initial assessment within seven.

Use the same private channel if you are the represented person or an authorized
representative requesting withdrawal, deletion, correction, or investigation
of impersonation or mistakenly collected material. Include only enough
information to locate the affected public artifact; the maintainer will arrange
a safer verification route before requesting identity or authorization records.

Good-faith research that avoids privacy violations, service disruption,
persistence, social engineering, and access to other people's data will not be
treated as hostile. Stop testing and report immediately if you encounter a
credential, consent record, private persona artifact, or real-person media.

If a credential may have been exposed, revoke and rotate it immediately;
deleting a file or issue is not sufficient. See `docs/SECURITY.md` for the
runtime threat model and consent boundaries.
