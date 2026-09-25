# Requirements Document

## Introduction

This feature extends the existing Trikon Cloud Webhook Receiver (Spec 1, located at `.kiro/specs/trikon-cloud-webhook-receiver/`) so that GitHub App **installation lifecycle** webhooks — `installation` and `installation_repositories` — are validated, mapped to a strongly-typed `InstallationEventMessage`, and enqueued on the separate `trikon-cloud-installation-events` SQS queue owned by Spec 3 (`trikon-cloud-orchestrator`).

Today the Webhook Receiver accepts only `ping` and `pull_request` events, forwards `pull_request` payloads to the Verify Jobs Queue, and drops everything else with HTTP 204. This spec adds a second enqueue path that runs **after** HMAC verification but **before** the current `pull_request` router, without altering any existing behavior. Spec 1 currently ships 49 tests at 96.44% coverage; those tests SHALL remain green.

The message shape is **not** redefined here — the handler imports `InstallationEventMessage` from `trikon_cloud.installation_lifecycle.models` (Spec 3), so producer and consumer share a single source of truth. The Installation Events Queue is created by Spec 3's `OrchestratorStack` and is referenced by ARN in Spec 1's CDK stack; this spec does not create the queue.

## Glossary

- **Webhook Receiver**: The Lambda function `trikon_cloud.webhook_receiver.handler`, entry point for all GitHub App webhooks in the Trikon Cloud control plane.
- **Installation Event**: A GitHub webhook whose `X-GitHub-Event` header is `installation` or `installation_repositories`, and whose payload `action` is one of the four values `created`, `deleted`, `added`, `removed` (in the combinations enumerated in Requirement 1).
- **Installation Events Queue**: The SQS queue named `trikon-cloud-installation-events`, created and owned by Spec 3's `OrchestratorStack`. Its URL is exposed to the Webhook Receiver via the environment variable `TRIKON_INSTALLATION_EVENTS_QUEUE_URL`.
- **Verify Jobs Queue**: The SQS queue named `trikon-cloud-verify-jobs`, created and owned by Spec 1's `WebhookReceiverStack`. Its URL is exposed via the environment variable `TRIKON_VERIFY_JOBS_QUEUE_URL`. Unchanged by this spec.
- **Event-Type Router**: The private helper `_route_event` inside `trikon_cloud/webhook_receiver/handler.py` that maps `(X-GitHub-Event, payload.action)` to a route enum (`enqueue_verify`, `enqueue_installation`, `pong`, `non_enqueue`). Extended by this spec.
- **Payload Mapping Helper**: A new private function `_build_installation_message` inside `trikon_cloud/webhook_receiver/handler.py` that transforms a validated installation payload into an `InstallationEventMessage`, applying the field mapping defined in Requirement 2.
- **SqsWriter**: The class `trikon_cloud.webhook_receiver.sqs_writer.SqsWriter`. Refactored by this spec to accept its target queue URL at construction time, so two independent instances (one per queue) can coexist as module-level singletons.

## Requirements

### Requirement 1: Installation Event Routing

**User Story:** As a Trikon Cloud operator, I want the Webhook Receiver to route GitHub installation lifecycle webhooks to a dedicated queue, so that the Orchestrator (Spec 3) can react to app-install and repository-selection changes without polluting the verification pipeline.

#### Acceptance Criteria

1. WHEN the Webhook Receiver receives an HTTP POST with a valid HMAC signature and header `X-GitHub-Event: installation` and payload field `action == "created"`, THE Webhook Receiver SHALL classify the request as route `enqueue_installation`.
2. WHEN the Webhook Receiver receives an HTTP POST with a valid HMAC signature and header `X-GitHub-Event: installation` and payload field `action == "deleted"`, THE Webhook Receiver SHALL classify the request as route `enqueue_installation`.
3. WHEN the Webhook Receiver receives an HTTP POST with a valid HMAC signature and header `X-GitHub-Event: installation_repositories` and payload field `action == "added"`, THE Webhook Receiver SHALL classify the request as route `enqueue_installation`.
4. WHEN the Webhook Receiver receives an HTTP POST with a valid HMAC signature and header `X-GitHub-Event: installation_repositories` and payload field `action == "removed"`, THE Webhook Receiver SHALL classify the request as route `enqueue_installation`.
5. WHEN the Webhook Receiver receives an HTTP POST with header `X-GitHub-Event: installation` or `X-GitHub-Event: installation_repositories` and a payload field `action` value that is not one of the four values enumerated in acceptance criteria 1 through 4, THE Webhook Receiver SHALL classify the request as route `non_enqueue` and SHALL respond with HTTP 204 and an empty body.
6. IF the HMAC signature over the raw request body does not match the `X-Hub-Signature-256` header, THEN THE Webhook Receiver SHALL reject the request with HTTP 401 before evaluating the Event-Type Router.
7. THE Event-Type Router SHALL preserve the existing routing decisions for `X-GitHub-Event: ping` (`pong`), `X-GitHub-Event: pull_request` with action in `opened`, `synchronize`, `reopened` (`enqueue_verify`), and all other event/action combinations (`non_enqueue`).

### Requirement 2: Payload Mapping to InstallationEventMessage

**User Story:** As a downstream consumer on the Installation Events Queue, I want each enqueued message to conform to `InstallationEventMessage` with byte-for-byte-preserved GitHub values, so that the Orchestrator can process the event without re-parsing GitHub payloads.

#### Acceptance Criteria

1. WHEN the Webhook Receiver classifies a request as `enqueue_installation`, THE Payload Mapping Helper SHALL construct an `InstallationEventMessage` imported from `trikon_cloud.installation_lifecycle.models`.
2. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage`, THE Payload Mapping Helper SHALL set field `installation_id` to the integer value of `payload.installation.id`.
3. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage`, THE Payload Mapping Helper SHALL set field `github_app_id` to the integer value of `payload.installation.app_id`.
4. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage`, THE Payload Mapping Helper SHALL set field `event_type` to the string produced by concatenating the `X-GitHub-Event` header, a single ASCII period, and the `payload.action` value.
5. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage` and the classified route derives from `X-GitHub-Event: installation` with `action == "created"`, THE Payload Mapping Helper SHALL set field `repositories` to a tuple built from the top-level `payload.repositories[*].full_name` list, preserving order.
6. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage` and the classified route derives from `X-GitHub-Event: installation` with `action == "deleted"`, THE Payload Mapping Helper SHALL set field `repositories` to the empty tuple `()`.
7. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage` and the classified route derives from `X-GitHub-Event: installation_repositories` with `action == "added"`, THE Payload Mapping Helper SHALL set field `repositories` to a tuple built from `payload.repositories_added[*].full_name`, preserving order.
8. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage` and the classified route derives from `X-GitHub-Event: installation_repositories` with `action == "removed"`, THE Payload Mapping Helper SHALL set field `repositories` to a tuple built from `payload.repositories_removed[*].full_name`, preserving order.
9. WHEN the Payload Mapping Helper constructs an `InstallationEventMessage`, THE Payload Mapping Helper SHALL set field `delivery_id` to the value of the `X-GitHub-Delivery` request header.
10. WHEN the Webhook Receiver enters its Lambda handler function, THE Webhook Receiver SHALL capture a single UTC timestamp by calling `datetime.now(UTC).isoformat(timespec="milliseconds")` and SHALL use that captured value as the `sent_at` field of any `InstallationEventMessage` constructed during the same invocation.
11. WHEN the Payload Mapping Helper copies a string value from the GitHub payload into an `InstallationEventMessage`, THE Payload Mapping Helper SHALL preserve the value byte-for-byte, without case folding, whitespace trimming, or Unicode normalization.

### Requirement 3: Malformed Installation Payload Rejection

**User Story:** As a Trikon Cloud operator, I want structurally invalid installation payloads to fail loudly with HTTP 400, while payloads carrying an unknown action follow the existing silent-drop convention, so that GitHub redeliveries surface real bugs and unrelated new actions do not generate alerts.

#### Acceptance Criteria

1. IF an HTTP POST has header `X-GitHub-Event: installation` or `X-GitHub-Event: installation_repositories` and a valid HMAC signature, and the request body fails Pydantic validation against the installation payload model, THEN THE Webhook Receiver SHALL respond with HTTP 400 and a structured JSON error body containing keys `error` and `detail`.
2. IF an HTTP POST has header `X-GitHub-Event: installation` or `X-GitHub-Event: installation_repositories` and a valid HMAC signature, and the request body is not valid JSON, THEN THE Webhook Receiver SHALL respond with HTTP 400 and a structured JSON error body containing keys `error` and `detail`.
3. IF an HTTP POST is classified as `enqueue_installation` per Requirement 1 and the `X-GitHub-Delivery` header is missing or empty, THEN THE Webhook Receiver SHALL respond with HTTP 400 and a structured JSON error body containing keys `error` and `detail`.
4. WHEN the Webhook Receiver responds with HTTP 400 per this requirement, THE Webhook Receiver SHALL NOT send any message to the Installation Events Queue or the Verify Jobs Queue.

### Requirement 4: SqsWriter Refactor and Dual-Instance Wiring

**User Story:** As the Webhook Receiver implementation, I need a reusable `SqsWriter` that binds to a specific queue at construction, so that a single Lambda invocation can send messages to either the Verify Jobs Queue or the Installation Events Queue without conditional queue-URL logic scattered through the handler.

#### Acceptance Criteria

1. THE `SqsWriter` class SHALL accept a `queue_url: str` positional or keyword argument in its `__init__` method and SHALL bind that URL to the instance for the lifetime of the instance.
2. WHEN the `SqsWriter.send` method is invoked on an instance, THE `SqsWriter` SHALL send the message to the queue URL supplied at construction time and SHALL NOT read any queue URL from environment variables at send time.
3. THE module `trikon_cloud.webhook_receiver.handler` SHALL construct exactly one `SqsWriter` bound to the Verify Jobs Queue URL and exactly one `SqsWriter` bound to the Installation Events Queue URL, both as module-level singletons initialized at Lambda cold start.
4. THE `ReceiverEnvConfig` Pydantic model SHALL define a field `installation_events_queue_url: str` with alias `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` and no default value.
5. IF the environment variable `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` is missing or empty at Lambda cold start, THEN THE Webhook Receiver SHALL raise `pydantic.ValidationError` during `ReceiverEnvConfig` instantiation, mirroring the existing behavior for `TRIKON_VERIFY_JOBS_QUEUE_URL`.
6. WHEN the Webhook Receiver classifies a request as `enqueue_installation` and successfully builds an `InstallationEventMessage`, THE Webhook Receiver SHALL serialize the message to JSON and SHALL send the serialized body to the Installation Events Queue via the Installation Events Queue `SqsWriter` instance, and SHALL respond with HTTP 202 on successful send.
7. WHEN the Webhook Receiver classifies a request as `enqueue_verify`, THE Webhook Receiver SHALL continue to send the message to the Verify Jobs Queue `SqsWriter` instance, unchanged from Spec 1.

### Requirement 5: CDK Stack Wiring for the Installation Events Queue

**User Story:** As a Trikon Cloud operator deploying the Webhook Receiver, I want the CDK stack to inject the Installation Events Queue URL into the Lambda's environment and grant the Lambda `sqs:SendMessage` on that queue, so that the deployed function has the exact permissions it needs and no more.

#### Acceptance Criteria

1. THE `WebhookReceiverStack` constructor SHALL accept a keyword argument `installation_events_queue_arn: str`.
2. WHEN `WebhookReceiverStack` is synthesized, THE `WebhookReceiverStack` SHALL import the Installation Events Queue by ARN using `aws_cdk.aws_sqs.Queue.from_queue_arn` and SHALL NOT declare a new `aws_sqs.Queue` construct for that queue.
3. WHEN `WebhookReceiverStack` is synthesized, THE `WebhookReceiverStack` SHALL set the environment variable `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` on the Webhook Receiver Lambda function to the imported queue's `queue_url` attribute.
4. WHEN `WebhookReceiverStack` is synthesized, THE `WebhookReceiverStack` SHALL grant the Webhook Receiver Lambda's execution role the `sqs:SendMessage` action on the imported Installation Events Queue ARN, and SHALL NOT grant any additional SQS actions on that queue.
5. THE CDK application entry point `app.py` SHALL read a CDK context value `installation_events_queue_arn` and SHALL pass it to `WebhookReceiverStack` via the `installation_events_queue_arn` keyword argument.
6. IF the CDK context value `installation_events_queue_arn` is missing or empty at synthesis time, THEN `app.py` SHALL raise a `ValueError` with a message identifying the missing context key.
7. THE `WebhookReceiverStack` SHALL preserve every existing environment variable, IAM grant, and resource declaration for the Verify Jobs Queue unchanged.

### Requirement 6: Non-Regression and Test Coverage

**User Story:** As a Trikon Cloud maintainer, I want the existing 49 Webhook Receiver tests to keep passing and new coverage to protect the new routing path, so that this extension does not silently break `pull_request` verification or drop test coverage below the current bar.

#### Acceptance Criteria

1. WHEN the full test suite `trikon_cloud/webhook_receiver/tests/` is executed with `pytest`, THE Webhook Receiver test suite SHALL report zero failures across all existing 49 test cases from Spec 1.
2. THE test file `trikon_cloud/webhook_receiver/tests/test_installation_events_routing.py` SHALL be added by this spec and SHALL contain at least 15 test cases.
3. THE new test file SHALL contain at least one test case that asserts route `enqueue_installation` for each of the four accepted event-action combinations enumerated in Requirement 1 acceptance criteria 1 through 4.
4. THE new test file SHALL contain at least one test case that asserts HTTP 400 for a Pydantic-validation-failing installation payload with a valid HMAC signature.
5. THE new test file SHALL contain at least one test case that asserts HTTP 204 for a `X-GitHub-Event: installation` request with an unknown action value and a valid HMAC signature.
6. THE new test file SHALL contain at least one test case that asserts HTTP 401 for a `X-GitHub-Event: installation` request with an invalid HMAC signature, demonstrating that HMAC verification runs before installation-event routing.
7. THE new test file SHALL contain at least one test case that asserts the Installation Events Queue `SqsWriter` instance is invoked (and the Verify Jobs Queue `SqsWriter` instance is not invoked) when the request is classified as `enqueue_installation`, and one test case that asserts the inverse for a `pull_request` request.
8. WHEN coverage is measured on `trikon_cloud/webhook_receiver/handler.py`, THE Webhook Receiver test suite SHALL report line coverage of at least 90 percent.

### Requirement 7: Cross-Cutting Invariants

**User Story:** As a Trikon Cloud engineer, I want this spec to respect the product-wide invariants — Python 3.11, strict typing, no new runtime dependencies, and no drift in the product name — so that the new code is indistinguishable in style from the existing Webhook Receiver.

#### Acceptance Criteria

1. THE Webhook Receiver source files modified or added by this spec SHALL type-check cleanly under `mypy --strict` using the project's existing configuration.
2. THE public function signatures added or modified by this spec SHALL NOT use `dict[str, Any]` or `Any` as parameter or return types on any function or method exported from `trikon_cloud.webhook_receiver`.
3. THE identifier `Trikon` (or `trikon_cloud`) SHALL be used consistently in every new module name, class name, environment variable name, IAM policy statement, and user-facing string introduced by this spec, and no alternate product name SHALL be introduced.
4. THE HMAC verification step defined by Spec 1 SHALL execute before the Event-Type Router evaluates installation events, and this spec SHALL NOT modify the HMAC verification implementation.
5. THE `pyproject.toml` file SHALL NOT gain any new runtime or development dependency as a result of this spec.
6. THE Spec 3 source files under `trikon_cloud/installation_lifecycle/` SHALL NOT be modified by this spec, with the sole permitted interaction being an `import` of `InstallationEventMessage` from `trikon_cloud.installation_lifecycle.models`.
7. THE Spec 3 `OrchestratorStack` and its CDK synthesis output SHALL NOT be modified by this spec.
