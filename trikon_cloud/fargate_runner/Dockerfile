# syntax=docker/dockerfile:1
# Trikon Cloud — Fargate verify-runner container (Spec 2 of M1).
# Extends the SDK's sandbox image with a thin entrypoint layer.

FROM suryansh639/trikon:0.4.1

WORKDIR /app

# Copy the runner package (production files; tests + infra excluded via .dockerignore)
COPY trikon_cloud/fargate_runner /app/trikon_cloud/fargate_runner
COPY trikon_cloud/__init__.py /app/trikon_cloud/__init__.py

# Install runtime deps. `trikon==0.4.1` matches the base image's SDK version exactly.
# --no-cache-dir keeps the image thin.
RUN pip install --no-cache-dir --disable-pip-version-check \
    "httpx>=0.27,<0.29" \
    "PyJWT[crypto]>=2.9,<3" \
    "pydantic>=2.9,<3" \
    "pydantic-settings>=2,<3" \
    "boto3>=1.35,<2" \
    "botocore>=1.35,<2" \
    "structlog>=24,<26" \
    "trikon==0.4.1"

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# ENTRYPOINT is the runner module. No CMD — every arg arrives via env vars
# (memo §5.3 / design §5.4). Container exit code from main() is the task's
# reported exit code.
ENTRYPOINT ["python", "-m", "trikon_cloud.fargate_runner.entrypoint"]
