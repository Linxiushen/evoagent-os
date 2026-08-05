FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 evoagent \
    && mkdir -p /state /workspace \
    && chown -R evoagent:evoagent /state /workspace
USER evoagent
EXPOSE 8811
ENTRYPOINT ["evoagent-runtime"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8811", "--state-dir", "/state"]
