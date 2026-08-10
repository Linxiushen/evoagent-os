FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 forge \
    && mkdir -p /registry \
    && chown -R forge:forge /registry
USER forge
EXPOSE 8822
ENTRYPOINT ["evoagent-forge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8822", "--registry", "/registry"]
