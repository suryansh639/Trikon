# syntax=docker/dockerfile:1
# Trikon GitHub Action image.
# Extends the sandbox base (`suryansh639/trikon:0.5.0` — python:3.11-slim +
# pytest/ruff/mypy pins in the system site-packages, plus the trikon CLI in
# its own venv at /opt/trikon behind a /usr/local/bin/trikon wrapper) with
# git and the action entrypoint. GitHub Actions builds this per-run when
# `image: Dockerfile` is set on the action; layer caching keeps subsequent
# runs fast.
#
# The action runs the CLI that ships in the base image rather than a second
# `pip install trikon`, so the action and the sandbox image always run the
# same trikon build with the same uv.lock-pinned dependencies, and those
# dependencies never mix with the consumer's packages, which entrypoint.sh
# installs into the system Python.
#
# GitHub passes no build args, so BASE_IMAGE keeps its default there. Override
# it only to test a locally built sandbox image, e.g.
#   docker build --build-arg BASE_IMAGE=trikon-sandbox:smoke -t trikon-action:smoke actions/verify

ARG BASE_IMAGE=suryansh639/trikon:0.5.0
FROM ${BASE_IMAGE}

# GitHub runs Docker actions as the default Docker user (root) and needs it to
# write the workspace and file-command mounts. The base image ends with
# `USER trikon`, so switch back.
USER root

# git: trikon's diff parser imports GitPython, which needs a git binary, and
# the sandbox base deliberately ships none (it runs untrusted code). With git
# on PATH the base wrapper's GIT_PYTHON_REFRESH=quiet default is harmless.
#
# The Debian package is not version-pinned. The base image is digest-pinned,
# but the Debian archive only serves the current build of each package in a
# release, so an exact `git=<version>` pin would break the build as soon as
# Debian ships a security update.
#
# safe.directory '*' (system scope, /etc/gitconfig): GitHub checks the
# workspace out as the runner user and mounts it at /github/workspace, while
# this container runs as root, so git refuses to work in it ("detected dubious
# ownership") and every verify would fail closed. Trade-off: '*' turns the
# ownership check off for the whole container instead of for one path. The
# check stops a user from running git inside a repository that another user
# controls, whose .git/config could run commands (core.fsmonitor and similar).
# It protects nothing here: the container is ephemeral, holds one checkout, and
# entrypoint.sh already runs that checkout's own code as root (pip install -e .,
# pytest). A build-time system entry is used instead of a per-path entry written
# by the entrypoint because a run-time `git config --global` write lands in
# $HOME, which GitHub mounts from the runner (/github/home) and shares with
# later container steps of the job, and because safe.directory must name the
# repository's top level, which is not the resolved repo path when `repo-path`
# points at a subdirectory of the checkout.
#
# Shadowing scripts: `trikon verify --no-sandbox` puts dirname(sys.executable)
# first on PATH (trikon/verify/local_sandbox.py and the host-side static
# baseline). For the bundled CLI that is /opt/trikon/bin, which also holds the
# console scripts of trikon's own pytest and coverage dependencies. That pytest
# has neither pytest-json-report nor the consumer's packages, so it would
# shadow /usr/local/bin/pytest and every run would fail closed. trikon never
# imports pytest or coverage and never runs these scripts by path, so removing
# them only changes which tool PATH lookup finds; the packages stay installed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && rm -f /opt/trikon/bin/pytest /opt/trikon/bin/py.test /opt/trikon/bin/coverage* \
 && test -x /opt/trikon/bin/trikon

# Action entrypoint. Copied here so GitHub's action runtime can invoke it
# without another mount.
COPY entrypoint.sh /action/entrypoint.sh
RUN chmod +x /action/entrypoint.sh

# The image stays on root (see above). The trikon CLI + pytest work fine as
# root inside the ephemeral action container.

ENTRYPOINT ["/action/entrypoint.sh"]
