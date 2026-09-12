"""End-to-end integration test against the checked-in sample repo.

The sample repo (`examples/sample_repo/`) is a tiny Python project with:
    - a known good state
    - a known bad diff that breaks one specific test
    - a checked-in .trikon/policy.yaml

We apply the bad diff, run `trikon.verify()`, and assert the verdict is
`block` with the specific failing test named in the evidence.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="sample_repo not yet materialized; sandbox + runner not implemented")
def test_bad_diff_produces_block_verdict(tmp_path):
    """Applying the seeded bad diff must yield decision == 'block'."""
    raise NotImplementedError


@pytest.mark.skip(reason="sample_repo not yet materialized")
def test_clean_diff_produces_allow_verdict(tmp_path):
    """A refactor with green tests + LOW blast + clean lint → allow."""
    raise NotImplementedError
