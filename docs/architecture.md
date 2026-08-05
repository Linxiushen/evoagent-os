# Architecture

Forge separates authoring, assurance and distribution. The parser turns YAML frontmatter into a canonical manifest. The scanner compares statically inferred capabilities with author declarations. The evaluator loads the declared entrypoint in the build environment and applies YAML cases. The packager sorts paths, fixes ZIP timestamps and writes a canonical manifest, creating byte-for-byte reproducible artifacts.

Signing is detached Ed25519 over the artifact SHA-256. Registry releases are immutable by `(name, version)` and stored under their content digest. Search uses SQLite FTS5. The reference registry is intentionally simple and can be fronted by object storage and an identity-aware publish service.

Evolution does not modify a published package. It writes an evidence bundle with baseline score, feedback and acceptance gates for a new candidate copy and version.

