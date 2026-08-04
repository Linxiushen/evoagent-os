# Contributing to EchoWeave-RTC

Thank you for helping improve EchoWeave-RTC. Contributions are welcome when
they preserve the project's consent-first and fail-closed security model.

## Before opening an issue

- Search existing issues and documentation first.
- Use the structured bug or feature form.
- Never upload API keys, authorization records, private logs, model weights,
  real-person audio/video, biometric embeddings, or persona adapters.
- Report vulnerabilities through GitHub's private security advisory flow, not
  a public issue.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev,silero-v5]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
```

Run the browser contract tests with Node.js 22 or newer:

```powershell
node --check src/echoweave/web/app.js
node --check src/echoweave/web/mic-worklet.js
node tests/js/test_app_lifecycle.mjs
node tests/js/test_frontend_contract.mjs
node tests/js/test_mic_worklet.mjs
```

## Pull requests

Keep changes focused and include tests proportional to their risk. Document
protocol, configuration, deployment, or security behavior changes. Explain
latency and memory tradeoffs for work on the realtime path. New model adapters
must pin an official upstream revision, document the model license, validate
all remote responses, and retain bounded timeouts and concurrency.

All examples must use fictional personas or synthetic fixtures. A software
contribution must not imply permission to use a person's identity or media.
By submitting a contribution, you agree that it is licensed under Apache-2.0.
