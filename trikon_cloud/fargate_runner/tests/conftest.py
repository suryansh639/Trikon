# Factory helper signatures use ``dict[str, Any]`` to model the arbitrary
# JSON shapes GitHub's REST API emits (installation-token responses, Check
# Run responses, PR-comment responses). Under the repo's
# ``disallow_any_explicit = true`` mypy config, each such helper surfaces
# as an ``explicit-any`` error. Suppress at file scope — this is a test
# conftest, and the ``Any`` here is bounded to fixture data that flows
# into pytest tests, never into production modules.
# mypy: disable-error-code="explicit-any"
"""Shared pytest fixtures for the Trikon Cloud Fargate runner tests.

Provides:

* Canonical fixture data constants (installation id, repo full name,
  PR number, head/base SHAs, delivery id, app id, and a freshly
  generated RSA private key PEM) — inline Python per the design.md §8
  rule that fixture data lives as constants in this file, NOT as
  top-level ``.json`` files under ``tests/fixtures/``.
* :func:`make_verdict` — factory returning a well-formed
  :class:`trikon.evidence.report.Verdict` with populated
  :class:`ImpactSet`, :class:`VerificationReport`, and
  :class:`StaticReport` sub-objects. Callers override individual
  fields via keyword arguments to exercise specific code paths.
* :func:`make_env_vars` — factory returning the memo §5.4 env-var
  dict for a :class:`RunnerEnvConfig` construction.
* :func:`make_installation_token_response`,
  :func:`make_check_run_response`, :func:`make_comment_response` —
  factories returning canonical GitHub REST response bodies.
* :func:`mock_aws_env`, :func:`dynamodb_tables`,
  :func:`evidence_bucket`, :func:`secrets_manager_secret` — pytest
  fixtures that stand up moto-backed AWS credentials + control-plane
  resources.

**A note on the moto v5 nested-context pattern.** Each ``mock_aws()``
context in the ``dynamodb_tables`` / ``evidence_bucket`` /
``secrets_manager_secret`` fixtures below is scoped to its own
fixture. Tests that need resources from multiple fixtures should
decorate the test itself with ``@mock_aws()`` (or use a single
fixture that composes the setup). This is a known limitation
inherited from the Wave-5 dispatch and can be tightened in follow-up
Wave-6 work.

See ``.kiro/specs/trikon-cloud-fargate-runner/design.md`` §5, §13 for
the authoritative shapes referenced here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import boto3  # type: ignore[import-untyped]
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from moto import mock_aws

from trikon.evidence.report import (
    BlastBucket,
    Decision,
    Evidence,
    ImpactSet,
    StaticReport,
    TestReport,
    Verdict,
    VerificationReport,
)

# ---------------------------------------------------------------------------
# Canonical fixture data constants — design.md §5.1 shape.
# ---------------------------------------------------------------------------

INSTALLATION_ID: int = 12345678
REPO_FULL_NAME: str = "octocat/hello-world"
PR_NUMBER: int = 42
HEAD_SHA: str = "6aabf09b" + "0" * 32  # 40-char hex
BASE_SHA: str = "87f7a31b" + "0" * 32  # 40-char hex
DELIVERY_ID: str = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
APP_ID: int = 999999
EVENT_TYPE: str = "pull_request.opened"


def _generate_rsa_test_key() -> bytes:
    """Generate a fresh RSA-2048 private key at module load.

    Returns the PEM-encoded PKCS8 bytes suitable for the GitHub App
    JWT-signing path in :mod:`trikon_cloud.fargate_runner.github_client`.
    Called exactly once at module load so every test sees the same key.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )


APP_PRIVATE_KEY_PEM: bytes = _generate_rsa_test_key()


# ---------------------------------------------------------------------------
# Verdict factory — assembles a well-formed Verdict for tests.
# ---------------------------------------------------------------------------


def make_verdict(
    *,
    decision: Decision = "block",
    matched_rule: str | None = "test rule",
    blast_radius_numeric: float = 42.0,
    blast_radius_score: BlastBucket = "MEDIUM",
    new_errors: int = 3,
    new_warnings: int = 1,
    preexisting_errors: int = 7,
    audit_id: UUID | None = None,
    changed_files: list[str] | None = None,
) -> Verdict:
    """Build a well-formed :class:`Verdict` for tests.

    Populates the :class:`ImpactSet`, :class:`VerificationReport`,
    :class:`StaticReport`, and :class:`TestReport` sub-objects with
    valid values keyed on the parameters. Callers can override any
    subset to exercise specific code paths (property tests draw
    values from hypothesis strategies; unit tests pin specific
    branches).
    """
    if audit_id is None:
        audit_id = uuid4()
    if changed_files is None:
        changed_files = ["src/example.py"]

    impact = ImpactSet(
        changed_files=changed_files,
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_numeric=blast_radius_numeric,
        blast_radius_score=blast_radius_score,
    )
    static = StaticReport(
        tools_run=["ruff", "mypy"],
        new_errors=new_errors,
        new_warnings=new_warnings,
        preexisting_errors=preexisting_errors,
        findings=[],
    )
    tests = TestReport(
        status="passed",
        total=0,
        passed=0,
        failed=0,
        skipped=0,
        duration_ms=0,
        failures=[],
    )
    verification = VerificationReport(
        tests=tests,
        static=static,
        sandbox_ms=100,
        total_ms=100,
    )
    evidence = Evidence(
        change=impact,
        verification=verification,
        policy_results=[],
    )
    return Verdict(
        decision=decision,
        reason="test verdict",
        matched_rule=matched_rule,
        evidence=evidence,
        audit_id=audit_id,
        created_at=datetime.now(UTC),
        warnings=[],
        schema_version=2,
    )


# ---------------------------------------------------------------------------
# Env-var factory — assembles the memo §5.4 env dict.
# ---------------------------------------------------------------------------


def make_env_vars(
    *,
    installation_id: int = INSTALLATION_ID,
    repo_full_name: str = REPO_FULL_NAME,
    pr_number: int = PR_NUMBER,
    head_sha: str = HEAD_SHA,
    base_sha: str = BASE_SHA,
    event_type: str = EVENT_TYPE,
    delivery_id: str = DELIVERY_ID,
    app_id: int = APP_ID,
    app_private_key_secret_arn: str = (
        "arn:aws:secretsmanager:us-east-1:123456789012:"
        "secret:trikon-cloud/github-app-private-key-abcdef"
    ),
) -> dict[str, str]:
    """Return the memo §5.4 env dict for :class:`RunnerEnvConfig`.

    The returned dict is suitable for
    :meth:`pytest.MonkeyPatch.setenv` iteration in tests that need to
    construct a full :class:`RunnerEnvConfig` instance. Every field
    is stringified because environment variables are always strings.
    """
    return {
        "TRIKON_INSTALLATION_ID": str(installation_id),
        "TRIKON_REPO_FULL_NAME": repo_full_name,
        "TRIKON_PR_NUMBER": str(pr_number),
        "TRIKON_HEAD_SHA": head_sha,
        "TRIKON_BASE_SHA": base_sha,
        "TRIKON_EVENT_TYPE": event_type,
        "TRIKON_DELIVERY_ID": delivery_id,
        "TRIKON_APP_ID": str(app_id),
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN": app_private_key_secret_arn,
        "AWS_REGION": "us-east-1",
    }


# ---------------------------------------------------------------------------
# GitHub REST response factories — canonical body shapes.
# ---------------------------------------------------------------------------


def make_installation_token_response(
    *,
    token: str = "ghs_test_token_not_real",
    expires_at: str = "2099-12-31T23:59:59Z",
) -> dict[str, Any]:
    """Body shape for ``POST /app/installations/{id}/access_tokens``.

    GitHub returns additional fields (``permissions``, ``repository_selection``)
    but the runner reads only ``token`` — the extra keys are omitted here
    for readability and re-added by tests that exercise the ``extra="allow"``
    branch of :class:`GithubInstallationTokenResponse`.
    """
    return {"token": token, "expires_at": expires_at}


def make_check_run_response(check_run_id: int = 111) -> dict[str, Any]:
    """Body shape for ``POST /repos/.../check-runs``.

    Only ``id`` is populated because the runner reads only that field
    from the response (mapped onto
    :attr:`PrStateRow.last_check_run_id`). Tests that need the full
    Check Run shape (name, status, conclusion, output, ...) construct
    it themselves.
    """
    return {"id": check_run_id}


def make_comment_response(comment_id: int = 222) -> dict[str, Any]:
    """Body shape for ``POST /repos/.../issues/{pr_number}/comments``.

    Only ``id`` is populated because the runner reads only that field
    (mapped onto :attr:`PrStateRow.last_comment_id`).
    """
    return {"id": comment_id}


# ---------------------------------------------------------------------------
# Moto-backed AWS control-plane fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set testing AWS credentials so moto's mocks activate cleanly.

    monkeypatch is function-scoped, so this fixture is function-scoped
    too — moto's :func:`mock_aws` context lives inside each dependent
    fixture below so per-test isolation is guaranteed.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def dynamodb_tables(mock_aws_env: None) -> Iterator[dict[str, str]]:
    """Create moto-backed DynamoDB tables and yield their names.

    Provisions ``trikon_verdicts`` (composite key
    ``installation_id: N + sk: S``) and ``trikon_pr_state`` (key
    ``pr_key: S``) — the two tables the Fargate runner writes to at
    runtime. Yields a name-keyed dict so tests can look up either
    table without hardcoding literals.
    """
    del mock_aws_env  # dependency only; monkeypatch has fired by now
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")

        client.create_table(
            TableName="trikon_verdicts",
            KeySchema=[
                {"AttributeName": "installation_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "installation_id", "AttributeType": "N"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="trikon_pr_state",
            KeySchema=[
                {"AttributeName": "pr_key", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pr_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {
            "verdicts": "trikon_verdicts",
            "pr_state": "trikon_pr_state",
        }


@pytest.fixture
def evidence_bucket(mock_aws_env: None) -> Iterator[str]:
    """Create the moto-backed evidence S3 bucket and yield its name.

    Mirrors the CDK stack's ``trikon-cloud-evidence`` bucket. Tests
    that write objects into the bucket use the yielded name as the
    ``Bucket`` argument to boto3's ``put_object`` / ``get_object``.
    """
    del mock_aws_env  # dependency only; monkeypatch has fired by now
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="trikon-cloud-evidence")
        yield "trikon-cloud-evidence"


@pytest.fixture
def secrets_manager_secret(mock_aws_env: None) -> Iterator[str]:
    """Create a moto Secrets Manager secret with the RSA PEM; yield ARN.

    Mirrors the operator-provisioned
    ``trikon-cloud/github-app-private-key`` secret referenced by the
    CDK stack. Yields the complete ARN so tests can drive
    ``RunnerEnvConfig.app_private_key_secret_arn`` end-to-end without
    hardcoding an account number.
    """
    del mock_aws_env  # dependency only; monkeypatch has fired by now
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        resp = client.create_secret(
            Name="trikon-cloud/github-app-private-key",
            SecretString=APP_PRIVATE_KEY_PEM.decode("utf-8"),
        )
        yield str(resp["ARN"])
