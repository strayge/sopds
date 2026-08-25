FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN useradd --create-home --uid 10001 sopds

COPY requirements.freeze.txt ./
RUN python -m pip install --no-cache-dir -r requirements.freeze.txt

COPY src ./src

RUN mkdir -p /data /config /library \
    && chown -R sopds:sopds /data /app

USER sopds

EXPOSE 8000

ENTRYPOINT ["python", "-m", "sopds"]
CMD ["--config", "/config/config.toml"]
