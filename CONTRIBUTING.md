# Contributing

Useful contributions include a minimal example of an incorrect score, a malformed task that is accepted unexpectedly, a false-positive lint finding, or a clearer setup instruction.

## Local checks

With Python 3.10 or newer, run:

```sh
python3 bin/eval-runner.py self-test
python3 bin/golden-lint.py --self-test
python3 -m unittest discover -s tests -v
```

Use `python` when appropriate on your system. These checks make no model calls. Live provider experiments should be opt-in, separately budgeted, and described honestly in the pull request.

## Changes to evaluation behavior

Include a small task and candidate that reproduce the problem. Show the expected result, the result before your change, and the result afterward. Add a good control case so a fix does not simply reject everything. Test the actual CLI when exit codes, paths, or report structure change.

Do not weaken a test to accommodate a wrong result. Document a behavior change in the walkthrough or evaluation guide when it changes the public contract.

## Third-party material

Keep upstream licenses, copyright notices, source URLs, exact source revisions where available, and a description of local modifications. The root MIT license does not relicense third-party content. Do not submit private research, account settings, actual credentials, personal logs, or code you lack permission to publish.

## Review

Keep changes focused. A reviewer should be able to connect the reported problem to a reproduction and the fix. Do not treat a model verdict as sole evidence of correctness. The historical personal-runtime hooks are reference-only; changes to them need a separate runtime-integration proposal.
