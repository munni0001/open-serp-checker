<!-- Keep this short. Describe the *why* — the diff shows the *what*. -->

## Summary

<!-- One or two sentences on what this PR changes and why it's needed. -->

## Type

<!-- Delete the lines that don't apply. -->

- Bug fix
- New feature
- New SERP engine (see CONTRIBUTING.md § Adding a new SERP engine)
- New proxy provider shape
- Docs / tooling
- Refactor (no behavior change)

## Verification

<!-- How did you check this works? Delete lines that don't apply. -->

- [ ] `pytest -v` passes locally
- [ ] `ruff check src/ tests/` passes locally
- [ ] Added or updated tests for the changed behavior
- [ ] Ran the dashboard (`uvicorn src.main:app`) and exercised the affected path
- [ ] Updated relevant docs (`README.md`, `docs/engines.md`, `docs/proxy-providers.md`, `CONTRIBUTING.md`)

## Related issues

<!-- "Closes #123" or "Refs #123". If there's no issue, say so and link context (a discussion, a benchmark). -->

## Notes for the reviewer

<!-- Anything non-obvious: tradeoffs, follow-ups, screenshots for UI changes, benchmark numbers for engine/proxy changes. Delete this section if nothing to say. -->
