FROM --platform=$BUILDPLATFORM python:3.14-slim AS converter-downloader

ARG TARGETARCH

RUN ["sh", "-c", "test \"${TARGETARCH:-}\" = amd64"]
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl unzip \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --output /tmp/fbc.zip \
        https://github.com/rupor-github/fb2cng/releases/download/v1.6.1/fbc-linux-amd64.zip \
    && printf '%s  %s\n' \
        db137f258b918d55310b7acd9ed8ec485d1ba0af60d09e6675b74af8288ddba2 \
        /tmp/fbc.zip \
        | sha256sum --check --strict - \
    && unzip -q /tmp/fbc.zip -d /tmp/fbc \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --output /tmp/kindling-cli \
        https://github.com/ciscoriordan/kindling/releases/download/v0.38.0/kindling-cli-linux \
    && printf '%s  %s\n' \
        67b58ba9f5f7f3e2e7602ccf5c1232bb22a49f95532981645ea3c106b8656b05 \
        /tmp/kindling-cli \
        | sha256sum --check --strict - \
    && install -D --mode=0755 /tmp/fbc/fbc /out/fbc \
    && install -D --mode=0755 /tmp/kindling-cli /out/kindling-cli

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN useradd --create-home --uid 1000 sopds

COPY requirements.freeze.txt ./
RUN python -m pip install --no-cache-dir -r requirements.freeze.txt

COPY src ./src
RUN ["python", "-c", "from sopds.web.i18n import compile_catalogs_if_needed; compile_catalogs_if_needed(force=True)"]
COPY --from=converter-downloader /out/fbc /usr/local/bin/fbc
COPY --from=converter-downloader /out/kindling-cli /usr/local/bin/kindling-cli

RUN ["sh", "-c", "set -eu; fbc_version=$(fbc --version); case \"$fbc_version\" in 'fbc version 1.6.1 '*) ;; *) printf 'Unexpected fbc version: %s\\n' \"$fbc_version\" >&2; exit 1;; esac; test \"$(kindling-cli --version)\" = 'kindling 0.38.0'"]

RUN mkdir -p /data /config /library \
    && chown -R sopds:sopds /data /app

USER sopds

EXPOSE 8000

ENTRYPOINT ["python", "-m", "sopds"]
CMD ["--config", "/config/config.toml"]
