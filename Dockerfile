FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[silero-v5]" \
    && addgroup --system echoweave \
    && adduser --system --ingroup echoweave --home /nonexistent echoweave \
    && mkdir -p /app/runtime /app/personas \
    && chown -R echoweave:echoweave /app/runtime /app/personas

EXPOSE 8765
USER echoweave
CMD ["echoweave", "serve", "--host", "0.0.0.0", "--port", "8765"]
