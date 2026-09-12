"""Verdict formatters — serialize a `Verdict` to different consumer surfaces.

Available formatters (v0.1):
    json.py       Canonical machine-readable JSON.
    markdown.py   GitHub PR comment / check-run summary body.

Deferred to later versions:
    sarif.py      SARIF output for security-tool interop (v0.3).
    otel.py       OpenTelemetry span export (v0.3).
"""
