"""Head-to-head evaluation harness for two headless coding models.

Runs each frozen task once per model in a fresh headless session, scores the
outcome against a known result, and applies a fixed recommendation rule.
Task lists, briefs and raw run bundles are data and live outside the repo.
"""
