FROM python:3.12.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash curl git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
