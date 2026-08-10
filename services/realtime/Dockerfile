ARG PYTHON_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

FROM ${PYTHON_IMAGE} AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY constraints ./constraints
COPY src ./src
RUN python -m pip wheel \
      --constraint constraints/gateway.txt \
      --wheel-dir /wheels \
      ".[silero-v5]"

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

LABEL org.opencontainers.image.title="EchoWeave-RTC" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.source="https://github.com/Linxiushen/echoweave-rtc" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
COPY --from=wheel-builder /wheels /wheels
RUN python -c "import glob, subprocess, sys; wheels = glob.glob('/wheels/echoweave_rtc-*.whl'); assert len(wheels) == 1, wheels; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-index', '--find-links=/wheels', wheels[0] + '[silero-v5]'])" \
    && addgroup --system --gid 10001 echoweave \
    && adduser --system --uid 10001 --ingroup echoweave --home /nonexistent --no-create-home echoweave \
    && mkdir -p /app/runtime /app/personas \
    && chown echoweave:echoweave /app/runtime /app/personas \
    && rm -rf /wheels

EXPOSE 8765
USER echoweave
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/ready', timeout=3)"]
CMD ["echoweave", "serve", "--host", "0.0.0.0", "--port", "8765"]
