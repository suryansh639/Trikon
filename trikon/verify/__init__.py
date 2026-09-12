"""Verification Runner — execute impacted checks inside an isolated sandbox.

Pipeline:
    test_selector.select_tests()  → tests exercising the impacted symbols
    static_checks.run_static()    → ruff, mypy, custom linters on changed files
    runner.run_verification()     → orchestrates the above inside sandbox.execute()

The runner does not decide anything. It reports facts. The policy engine decides.
"""
