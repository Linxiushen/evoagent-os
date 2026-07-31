# Consent and synthetic-media safety

EchoWeave is designed for authorized digital twins, fictional characters and
clearly disclosed synthetic media. It must not be used to impersonate a person,
mislead an audience, bypass identity checks, commit fraud or produce
disinformation.

## Enforced in code

- The public realtime endpoint accepts a `persona_id`, not arbitrary biometric
  uploads.
- Every on-disk profile requires signed scope for conversation, persona,
  voice cloning and avatar animation. Hosted DeepSeek use additionally
  requires `third_party_model_processing`.
- Real-person profiles require a verification record ID, expiry, non-withdrawn
  status and SHA-256 binding for every reference asset.
- HMAC consent signatures are required by default.
- Each session keeps its own immutable authorization snapshot. A new signed
  manifest cannot silently change the baseline of an existing session.
- The exact bytes used for the Nuwa profile, reference face and reference voice
  are hash-verified during one read and captured in that snapshot; active
  sessions never consume later replacements from mutable persona paths.
- Signed highest revisions and irreversible withdrawal tombstones are stored
  in the HMAC-authenticated `ECHOWEAVE_CONSENT_STATE_PATH`.
- The model prompt must not claim to be the real person.
- The UI contains a persistent `AI 数字分身 / SYNTHETIC MEDIA` overlay, and
  the SoulX worker burns `AI DIGITAL TWIN` into every returned MP4 segment.
- Secrets are read only from server environment variables.

## Operator responsibilities

The software cannot determine whether a consent document is truthful. The
operator must perform identity/authority verification, maintain revocation,
honor deletion, minimize stored biometric data and keep an audit trail. Add
metadata provenance to exported files; the fixed visual watermark is not a
substitute for C2PA-style export provenance.

The bundled consent state is designed for one gateway process and survives
ordinary restarts. It is not a transparency log or a multi-replica database:
rolling back both the manifest and state volume, sharing the JSON file between
multiple writers, or losing the volume defeats its monotonic guarantee. Use an
external authoritative consent/revocation store with transactional monotonic
updates for multiple replicas or higher-assurance deployments. A withdrawn
`consent_id` is never reusable; a genuinely new authorization needs a new ID.

Do not reuse the API key shown in any chat, issue tracker or terminal log. Revoke
it, create a new one, and inject the replacement through a secret manager or
`DEEPSEEK_API_KEY` on the server.

## Deployment controls

- HTTPS/WSS, authentication and per-tenant authorization.
- Short-lived session tokens, rate limits and request-size limits.
- Isolated model workers with no arbitrary outbound network access.
- No default recording of raw calls or biometric reference material.
- Tool calls through an allowlisted broker; external side effects need explicit
  user confirmation and idempotency keys.
