# syntax=docker/dockerfile:1
# Trikon Cloud — Fargate verify-runner container (Spec 2 of M1).
# Extends the SDK's sandbox image with a thin entrypoint layer.
#
# BASE_IMAGE exists only so a locally built sandbox image can be tested, e.g.
#   docker build --build-arg BASE_IMAGE=trikon-sandbox:smoke \
#     -f trikon_cloud/fargate_runner/Dockerfile -t trikon-fargate:smoke .
# Release builds use the default.

ARG BASE_IMAGE=suryansh639/trikon:0.5.0
FROM ${BASE_IMAGE}

# git: git_ops.py shallow-fetches the target repo with git subprocesses, and
# trikon's diff parser imports GitPython, which needs a git binary. The sandbox
# base deliberately ships none, because it runs untrusted code. The Debian
# package is not version-pinned: the base image is digest-pinned, but the
# Debian archive only serves the current build of each package in a release,
# so an exact `git=<version>` pin would break the build as soon as Debian
# ships a security update. No safe.directory entry is needed: the runner
# clones into a directory it creates itself, as the same user that runs git.
# apt needs root; switch back to the base image's `trikon` user afterwards so
# the steps below and the runner process run as before.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
USER trikon

WORKDIR /app

# Copy the runner package (production files; tests + infra excluded via .dockerignore)
COPY trikon_cloud/fargate_runner /app/trikon_cloud/fargate_runner
COPY trikon_cloud/__init__.py /app/trikon_cloud/__init__.py

# Install runtime deps. `trikon==0.5.0` matches the base image's SDK version exactly.
# --no-cache-dir keeps the image thin.
RUN pip install --no-cache-dir --disable-pip-version-check \
    "httpx>=0.27,<0.29" \
    "PyJWT[crypto]>=2.9,<3" \
    "pydantic>=2.9,<3" \
    "pydantic-settings>=2,<3" \
    "boto3>=1.35,<2" \
    "botocore>=1.35,<2" \
    "structlog>=24,<26" \
    "trikon==0.5.0"

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# ENTRYPOINT is the runner module. No CMD — every arg arrives via env vars
# (memo §5.3 / design §5.4). Container exit code from main() is the task's
# reported exit code.
ENTRYPOINT ["python", "-m", "trikon_cloud.fargate_runner.entrypoint"]
