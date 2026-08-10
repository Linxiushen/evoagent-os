FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 fleet \
    && mkdir -p /state \
    && chown -R fleet:fleet /state
USER fleet
EXPOSE 8833
ENTRYPOINT ["evoagent-fleet"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8833", "--state-dir", "/state"]
