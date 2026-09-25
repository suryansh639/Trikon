# DynamoDB / S3 attribute-value dicts are the boto3 wire shape, which is
# untyped upstream. Under this repo's ``disallow_any_explicit = true`` mypy
# config the ``Any`` annotations that surface at the boto3 boundary would
# otherwise fail ``--strict``. The DI seams on ``DynamoDBWriter.__init__``
# also accept ``Any | None`` so tests can pass moto clients without a
# Protocol adapter. Silence at the file level — the ``Any`` uses are
# scoped to boto3 return shapes and method bodies, not to public
# non-boto3 signatures (Requirement 13.3 talks about hand-rolled public
# APIs, which continue to be typed precisely).
# mypy: disable-error-code="explicit-any"
"""DynamoDB writer with conditional PutItem + S3 spill (design.md §8).

Wraps the three data-plane touchpoints the Fargate runner makes into
one class:

* ``dynamodb:GetItem`` / ``dynamodb:PutItem`` on ``trikon_pr_state`` —
  read the last-known Check Run + comment IDs, then upsert the row
  with the current invocation's IDs.
* ``dynamodb:PutItem`` on ``trikon_verdicts`` with the
  ``attribute_not_exists(installation_id) AND attribute_not_exists(sk)``
  condition — the load-bearing idempotency mechanism for Property 1
  (``Idempotency_On_Natural_Key``). On ``ConditionalCheckFailedException``
  the writer surfaces :class:`VerdictAlreadyCommittedError` (a subclass
  of :class:`VerdictWriteError`) so ``entrypoint.main()`` can log INFO
  and exit 0 without re-posting to GitHub (Requirement 5.3).
* ``s3:PutObject`` on ``trikon-cloud-evidence`` — the spill path for
  gzipped evidence blobs that exceed the ~350 KB inline guardrail
  (design.md §3.9 clarify answer 5). Object key format:
  ``<installation_id>/<audit_id>.json.gz``.

Public surface:

* :class:`DynamoDBWriter` — the wrapper. Constructor accepts injected
  boto3 clients for tests (moto-backed) and lazily builds real ones
  otherwise.
* :class:`VerdictWriteError` — generic failure surface.
* :class:`VerdictAlreadyCommittedError` — subclass fired only on the
  natural-key idempotency win.

**Tenant isolation** (Invariant 1): every operation this class
performs is scoped to the caller's installation via the per-task IAM
role's ``LeadingKeys`` / prefix conditions (design.md §9). The
application layer never has to check — a compromised container
running with installation A's role attempting a write for
installation B receives ``AccessDenied`` from IAM.
"""

from __future__ import annotations

import gzip
from typing import Any
from uuid import UUID

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trikon_cloud.fargate_runner.models import PrStateRow, VerdictRow

# Ordering places the class before its exceptions — deliberate, not
# alphabetical (mirrors sibling ``sqs_writer`` in webhook_receiver).
__all__ = [  # noqa: RUF022
    "DynamoDBWriter",
    "VerdictWriteError",
    "VerdictAlreadyCommittedError",
]


class VerdictWriteError(Exception):
    """Raised when a DynamoDB / S3 write fails during verdict persistence."""


class VerdictAlreadyCommittedError(VerdictWriteError):
    """Raised when the conditional PutItem finds an existing row.

    Signals the natural-key idempotency win: a prior invocation
    committed the verdict for this ``(installation_id, sk)`` pair —
    the current run exits 0 without re-posting to GitHub
    (Requirement 5.3).
    """


class DynamoDBWriter:
    """Writes verdicts + PR state to DynamoDB, with S3 spill for large evidence.

    All writes are scoped to a single installation's rows via the
    per-installation IAM task role (Invariant 1). The writer does not
    attempt to enforce tenant isolation at the application layer —
    that is entirely the IAM boundary's job.

    ``dynamodb_client`` and ``s3_client`` are dependency-injection
    seams for tests. When ``None`` (the production path), the class
    lazily constructs boto3 clients using ambient credentials.
    """

    def __init__(
        self,
        *,
        verdicts_table_name: str,
        pr_state_table_name: str,
        evidence_bucket_name: str,
        dynamodb_client: Any | None = None,
        s3_client: Any | None = None,
    ) -> None:
        self._verdicts_table_name = verdicts_table_name
        self._pr_state_table_name = pr_state_table_name
        self._evidence_bucket_name = evidence_bucket_name
        self._dynamodb = (
            dynamodb_client if dynamodb_client is not None else boto3.client("dynamodb")
        )
        self._s3 = s3_client if s3_client is not None else boto3.client("s3")

    def get_pr_state(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        pr_number: int,
    ) -> PrStateRow | None:
        """Fetch the current PR-state row, or ``None`` if not present.

        Constructs the ``pr_key`` composite (``<installation_id>#
        <repo_full_name>#<pr_number>``) and issues a
        ``dynamodb:GetItem``. On a miss (no ``Item`` in the response)
        returns ``None`` so the caller falls through to the create
        path. Any ``ClientError`` wraps as :class:`VerdictWriteError`.
        """
        pr_key = f"{installation_id}#{repo_full_name}#{pr_number}"
        try:
            response = self._dynamodb.get_item(
                TableName=self._pr_state_table_name,
                Key={"pr_key": {"S": pr_key}},
            )
        except ClientError as exc:
            raise VerdictWriteError(
                f"get_pr_state failed for {pr_key}: {exc}"
            ) from exc
        item = response.get("Item")
        if item is None:
            return None
        return PrStateRow(
            pr_key=pr_key,
            last_comment_id=(
                int(item["last_comment_id"]["N"])
                if "last_comment_id" in item
                and item["last_comment_id"].get("NULL") is not True
                else None
            ),
            last_check_run_id=(
                int(item["last_check_run_id"]["N"])
                if "last_check_run_id" in item
                and item["last_check_run_id"].get("NULL") is not True
                else None
            ),
            last_head_sha=item["last_head_sha"]["S"],
            last_updated_at=item["last_updated_at"]["S"],
        )

    def put_verdict(self, *, row: VerdictRow) -> None:
        """Write a verdict row via conditional PutItem (design.md §8).

        Builds the DynamoDB attribute-value item verbatim from
        design.md §8, including the ``evidence_blob`` / ``evidence_s3_key``
        exactly-one branch (enforced upstream by
        :class:`~trikon_cloud.fargate_runner.models.VerdictRow`'s
        ``model_validator``). The ``ConditionExpression`` is the
        load-bearing idempotency guard for Property 1.

        Raises:
            VerdictAlreadyCommittedError: On
                ``ConditionalCheckFailedException`` — a prior
                invocation won the race, and this run should not
                re-post to GitHub (Requirement 5.3).
            VerdictWriteError: On any other ``ClientError``.
        """
        item: dict[str, Any] = {
            "installation_id": {"N": str(row.installation_id)},
            "sk": {"S": row.sk},
            "repo_full_name": {"S": row.repo_full_name},
            "pr_number": {"N": str(row.pr_number)},
            "head_sha": {"S": row.head_sha},
            "base_sha": {"S": row.base_sha},
            "decision": {"S": row.decision},
            "matched_rule": {"S": row.matched_rule},
            "blast_radius_score": {"N": str(row.blast_radius_score)},
            "new_errors": {"N": str(row.new_errors)},
            "new_warnings": {"N": str(row.new_warnings)},
            "preexisting_errors": {"N": str(row.preexisting_errors)},
            "duration_ms": {"N": str(row.duration_ms)},
            "fargate_task_arn": {"S": row.fargate_task_arn},
            "schema_version": {"N": str(row.schema_version)},
            "risk_bucket_sk": {"S": row.risk_bucket_sk},
        }
        # ``evidence_blob`` XOR ``evidence_s3_key`` — the ``VerdictRow``
        # model_validator guarantees exactly one is non-``None``.
        if row.evidence_blob is not None:
            item["evidence_blob"] = {"B": row.evidence_blob}
        else:
            # ``evidence_s3_key`` is guaranteed non-``None`` by the
            # model_validator when ``evidence_blob is None``.
            assert row.evidence_s3_key is not None
            item["evidence_s3_key"] = {"S": row.evidence_s3_key}

        try:
            self._dynamodb.put_item(
                TableName=self._verdicts_table_name,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(installation_id) "
                    "AND attribute_not_exists(sk)"
                ),
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ConditionalCheckFailedException":
                raise VerdictAlreadyCommittedError(
                    f"verdict row already exists for sk={row.sk!r}"
                ) from exc
            raise VerdictWriteError(
                f"put_verdict failed for sk={row.sk!r}: {exc}"
            ) from exc

    def upsert_pr_state(self, *, row: PrStateRow) -> None:
        """Upsert (overwrite) a ``trikon_pr_state`` row.

        No ``ConditionExpression`` — this is a last-known-state table,
        not append-only. Nullable IDs (``last_comment_id``,
        ``last_check_run_id``) map to DynamoDB's ``NULL`` attribute
        type so the reader in :meth:`get_pr_state` can round-trip them
        back to ``None``.

        Raises:
            VerdictWriteError: On any ``ClientError``.
        """
        item: dict[str, Any] = {
            "pr_key": {"S": row.pr_key},
            "last_head_sha": {"S": row.last_head_sha},
            "last_updated_at": {"S": row.last_updated_at},
        }
        if row.last_comment_id is not None:
            item["last_comment_id"] = {"N": str(row.last_comment_id)}
        else:
            item["last_comment_id"] = {"NULL": True}
        if row.last_check_run_id is not None:
            item["last_check_run_id"] = {"N": str(row.last_check_run_id)}
        else:
            item["last_check_run_id"] = {"NULL": True}

        try:
            self._dynamodb.put_item(
                TableName=self._pr_state_table_name,
                Item=item,
            )
        except ClientError as exc:
            raise VerdictWriteError(
                f"upsert_pr_state failed for pr_key={row.pr_key!r}: {exc}"
            ) from exc

    def spill_evidence_to_s3(
        self,
        *,
        installation_id: int,
        audit_id: UUID,
        gzipped_bytes: bytes,
    ) -> str:
        """Write gzipped evidence to S3; return the object key.

        Key format: ``<installation_id>/<audit_id>.json.gz`` — matches
        the per-installation IAM prefix condition in design.md §9 so
        the write is authorized by the ``LeadingKeys`` template.
        ``ContentType`` and ``ContentEncoding`` are set so downstream
        consumers (dashboard, compliance export) can stream the body
        without extra metadata calls.

        Raises:
            VerdictWriteError: On any ``ClientError``.
        """
        key = f"{installation_id}/{audit_id}.json.gz"
        try:
            self._s3.put_object(
                Bucket=self._evidence_bucket_name,
                Key=key,
                Body=gzipped_bytes,
                ContentType="application/json",
                ContentEncoding="gzip",
            )
        except ClientError as exc:
            raise VerdictWriteError(
                f"spill_evidence_to_s3 failed for {key}: {exc}"
            ) from exc
        return key

    @staticmethod
    def compute_evidence_blob(*, evidence_json: str) -> bytes:
        """Gzip a JSON string. Pure helper — no IO.

        Extracted as a ``@staticmethod`` so the caller in
        ``entrypoint.main()`` can compute the compressed size before
        deciding between the inline / spill branches (Requirement 6.1)
        without materializing a ``DynamoDBWriter`` instance.
        """
        return gzip.compress(evidence_json.encode("utf-8"))
