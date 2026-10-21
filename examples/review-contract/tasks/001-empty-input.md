---
id: 001-empty-input
type: deterministic
---

## Input

Review this proposed Python function. Its contract is to return the first element,
or None when the input is empty. Respond with a JSON review containing `decision`,
`finding.code`, `finding.line`, and `usage.total_tokens`.

```python
def first(items):
    return items[0]
```

## Reference

An empty list raises IndexError at line 2. The fixture's correct review asks for
changes, identifies EMPTY_INPUT at line 2, and records no more than 4,000 tokens.
The reference and both candidates are handwritten examples, not model outputs.

## Check

```json
{"checks": [
  {"name": "requires-changes", "type": "exact", "field": "decision", "value": "needs_changes"},
  {"name": "finding-code", "type": "exact", "field": "finding.code", "value": "EMPTY_INPUT"},
  {"name": "finding-line", "type": "exact", "field": "finding.line", "value": "2"},
  {"name": "review-budget", "type": "budget", "field": "usage.total_tokens", "value": 4000}
]}
```

## Limits

These checks compare a structured review to one known answer. They do not execute
the code, establish review quality on other programs, or verify supplied usage.
