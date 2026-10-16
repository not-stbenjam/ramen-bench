FROM python:3.12.12-slim-bookworm
COPY requirements.txt /tests/requirements.txt
RUN pip install --no-cache-dir -r /tests/requirements.txt
COPY . /tests/
WORKDIR /app
