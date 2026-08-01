# Supply-chain and reproducible release policy

EchoWeave treats Python packages, container bases, GitHub Actions, model
weights and model-serving code as independently reviewable artifacts. A model
repository name, semantic version or floating branch is not an immutable
release identity.

## Gateway dependencies

- `pyproject.toml` pins the isolated wheel build backend exactly.
- `constraints/gateway.txt` locks the Python 3.12 gateway plus Silero runtime
  resolution. `constraints/ci.txt` locks the test tools. Package metadata keeps
  compatible ranges so downstream library users can resolve normally; release
  and container installs must apply the constraints.
- The constraints prevent accidental upgrades, but they are not signatures.
  For an isolated or high-assurance deployment, download the selected wheels
  into a private immutable wheelhouse, malware-scan them, record SHA-256 for
  every file, and install only from that wheelhouse with `--no-index`.
- Never generate a production lock from an unreviewed workstation environment.
  Update constraints in a dedicated dependency-review change, run the complete
  suite, inspect license/security deltas and rebuild the performance baseline.

Example connected preparation followed by an offline install:

```bash
python -m pip download --only-binary=:all: \
  --constraint constraints/gateway.txt \
  --dest wheelhouse ".[silero-v5]"
sha256sum wheelhouse/* > wheelhouse/SHA256SUMS
python -m pip install --no-index --find-links=wheelhouse \
  --constraint constraints/gateway.txt ".[silero-v5]"
```

Store `SHA256SUMS` outside the mutable artifact bucket or sign it with the
organization's release-signing system. Do not commit third-party wheels to this
repository.

## Release wheel

CI builds the wheel twice with a fixed `SOURCE_DATE_EPOCH` and requires the two
files to be byte-identical. `scripts/verify_wheel.py` validates normalized name
and version, ZIP paths, RECORD hashes and sizes, required Python modules, and
all five self-hosted Web assets (`index.html`, `styles.css`, `app.js`,
`mic-worklet.js`, and `api.html`). It also rejects common secret/key and bytecode
suffixes.

The source version, image label and CI expected version are intentionally
checked by `tests/test_release_contract.py`. A release owner still has to sign
the final wheel digest and attach provenance in the publishing system; this
repository does not manufacture trust from an unsigned CI run.

## Container image

The Dockerfile pins the official Python multi-platform manifest by SHA-256,
builds a constrained wheelhouse in a separate stage, installs without reaching
an index in the runtime stage, and runs as fixed UID/GID 10001. The runtime
image contains no compiler or source tree and exposes a readiness healthcheck.
Compose adds a read-only root filesystem, no-new-privileges, all-capability
drop, PID ceiling and bounded `tmpfs`.

When updating Python, resolve and review the new official manifest digest for
the exact intended architecture set. Production deployments must reference the
built EchoWeave image by digest (`repo@sha256:...`), not by a mutable tag, and
should retain an SBOM and vulnerability scan alongside that digest.

## CI actions

The checked-in `ci/github-actions.yml` template gives only `contents: read` and
pins `checkout`, `setup-python`, and `setup-node` to full commit SHAs. Dependabot
or an equivalent reviewer may propose SHA updates, but a floating major tag
must not replace those pins. Copy the template to `.github/workflows/ci.yml`
only through a GitHub credential authorized to create workflow files.

## Model and worker artifacts

The audited full revisions are recorded in `docs/MODELS.md`. Download Hugging
Face snapshots by full commit, for example:

```bash
huggingface-cli download Qwen/Qwen3-ASR-1.7B \
  --revision 7278e1e70fe206f11671096ffdd38061171dd6e5 \
  --local-dir /models/Qwen3-ASR-1.7B
huggingface-cli download openbmb/VoxCPM2 \
  --revision bffb3df5a29440629464e5e839f4d214c8714c3d \
  --local-dir /models/VoxCPM2
huggingface-cli download Soul-AILab/SoulX-FlashHead-1_3B \
  --revision 59119b6c681230c3eeee157e224ae1941746711e \
  --local-dir /models/SoulX-FlashHead-1_3B
huggingface-cli download facebook/wav2vec2-base-960h \
  --revision 22aad52d435eb6dbaf354bdad9b0da84ce7d6156 \
  --local-dir /models/wav2vec2-base-960h
```

Hash the snapshot files, run workers with network-disabled/offline model loading
where practical, and record the worker container digest, CUDA/driver version,
precision and inference flags with benchmark results. Pin Nuwa and SoulX source
repositories to the reviewed Git commits rather than cloning `main` during a
deployment.

DeepSeek is a hosted service whose model identifier does not expose an immutable
weight digest. Treat any provider-side revision as an external change: run
contract, safety and latency acceptance tests before promotion, keep a rollback
route, and never send a persona unless its signed consent includes the hosted
processing scope.
