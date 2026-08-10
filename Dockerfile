FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVOAGENT_OS_STATE_DIR=/var/lib/evoagent-os

WORKDIR /opt/evoagent-os
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY apps/control-plane ./apps/control-plane
COPY packages/contracts ./packages/contracts
COPY services/runtime ./services/runtime
COPY services/fleet ./services/fleet
COPY services/forge ./services/forge
COPY services/observability ./services/observability
COPY services/realtime ./services/realtime

RUN pip install --no-cache-dir \
      ./packages/contracts \
      ./services/runtime \
      ./services/fleet \
      ./services/forge \
      ./services/observability \
      ./services/realtime \
      . \
    && mkdir -p /var/lib/evoagent-os \
    && chown -R 65532:65532 /var/lib/evoagent-os

USER 65532:65532
EXPOSE 8765
VOLUME ["/var/lib/evoagent-os"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)"

ENTRYPOINT ["evoagent-os"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
