# syntax=docker/dockerfile:1
# Trikon GitHub Action image.
# Extends the sandbox base (`suryansh639/trikon:0.4.1` — python:3.11-slim +
# pytest/ruff/mypy pins) with the trikon CLI and the action entrypoint.
# GitHub Actions builds this per-run when `image: Dockerfile` is set on
# the action; layer caching keeps subsequent runs fast.

FROM suryansh639/trikon:0.4.1

USER root

# Install the trikon CLI. Version-pinned to 0.4.1 to keep the action's
# behavior in lockstep with the sandbox image base above.
RUN pip install --no-cache-dir --disable-pip-version-check trikon==0.4.1

# Action entrypoint. Copied here so GitHub's action runtime can invoke it
# without another mount.
COPY entrypoint.sh /action/entrypoint.sh
RUN chmod +x /action/entrypoint.sh

# GitHub Actions overrides USER to root at runtime anyway; leave root here
# for clarity. The trikon CLI + pytest work fine as root inside the
# ephemeral action container.

ENTRYPOINT ["/action/entrypoint.sh"]
