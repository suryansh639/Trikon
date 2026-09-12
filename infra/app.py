"""CDK entrypoint for the Trikon hosted control plane.

Deferred to v0.2. Currently a stub. Mirrors Unideploy's infra/app.py layout so
whoever picks this up has the same shape to work from.

Deploy:
    cdk deploy TrikonApiStack
"""

from __future__ import annotations

import aws_cdk as cdk

from stacks.api_stack import TrikonApiStack


app = cdk.App()

TrikonApiStack(
    app,
    "TrikonApiStack",
    env=cdk.Environment(region="us-east-1"),
)

app.synth()
