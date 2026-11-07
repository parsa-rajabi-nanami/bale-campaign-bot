## Summary

<!-- Describe the change and user impact. -->

## Verification

- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `make test`
- [ ] `uv run pip-audit`
- [ ] Docs and migrations updated where needed

## Safety

- [ ] No secrets, subscriber data, or campaign content included
- [ ] Imported phones do not create subscriptions or message unmatched numbers
