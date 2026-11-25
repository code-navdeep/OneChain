# OneChain — Docker image
#
# Builds from requirements.lock (not requirements.txt) for bit-for-bit reproducible installs — the
# lock pins every package, indirect ones included, to the tested version (requirements.txt lists only
# the direct dependencies). Runtime data (data/) and secrets (.env) are NOT
# baked into this image — docker-compose.yml bind-mounts them at run time instead, so they survive
# rebuilds and restarts and stay directly editable on the host.
#
# SECURITY: HOST=0.0.0.0 below makes the app reachable through Docker's port publishing (a
# container's own 127.0.0.1 is otherwise unreachable from the host machine, since it's isolated
# inside the container's own network namespace). What keeps OneChain unreachable from any OTHER
# machine is docker-compose.yml pinning the published port to 127.0.0.1 on the host side — never
# remove that prefix there. See config.py's `host` field comment for the full reasoning.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock

COPY . .
RUN mkdir -p /app/data

ENV HOST=0.0.0.0
EXPOSE 8053

CMD ["python3", "app.py"]
