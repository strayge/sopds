#!/usr/bin/env sh
set -eu

uv pip compile \
  --group runtime \
  --python-version 3.14 \
  --universal \
  --output-file requirements.freeze.txt
uv pip compile \
  --group dev \
  --python-version 3.14 \
  --universal \
  --output-file requirements.dev.freeze.txt
