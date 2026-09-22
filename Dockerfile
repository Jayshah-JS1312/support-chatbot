FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUPPORT_CHATBOT_HOST=0.0.0.0 \
    SUPPORT_CHATBOT_STATE_DIR=/data/state \
    SUPPORT_CHATBOT_CACHE_DIR=/data/cache

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/state /data/cache \
    && chown -R app:app /data

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["support-chatbot-web"]
