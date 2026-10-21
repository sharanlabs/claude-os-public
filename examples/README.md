# Reproduce the evaluation contracts

Run from the repository root with Python 3.10 or newer. These examples use only the
standard library. They do not contact a provider, read credentials, or incur API cost.
Tasks, candidates, usage figures, and verdicts are handwritten fixtures.

Each task ID starts with an ASCII letter or digit and then contains only ASCII
letters, digits, underscores, or hyphens. IDs must be unique within a suite.
Candidate symlinks that resolve outside the candidate directory are rejected.

```sh
python3 bin/eval-runner.py run --tasks examples/review-contract/tasks --candidates examples/review-contract/candidates-good
```

Expected exit: **0**. Replace `candidates-good` with `candidates-bad` to see four
failed checks and exit **1**. The budget check records supplied token numbers;
the runner does not measure or verify token usage.

```sh
python3 bin/golden-lint.py examples/review-contract/tasks
python3 bin/golden-lint.py examples/judge/tasks
python3 bin/eval-runner.py run --tasks examples/judge/tasks --candidates examples/judge/candidates
```

The two lint commands exit **0**. The judge suite exits **2** because a judgment
is outstanding. Incomplete means incomplete, even when deterministic checks pass.

To see the full scoring prompt and then ingest a fixture verdict:

```sh
python3 bin/eval-runner.py judge-prompt --task examples/judge/tasks/002-review-explanation.md --candidate examples/judge/candidates/002-review-explanation.txt
python3 bin/eval-runner.py ingest-verdict --task examples/judge/tasks/002-review-explanation.md --verdict examples/judge/verdicts/good.json
python3 bin/eval-runner.py ingest-verdict --task examples/judge/tasks/002-review-explanation.md --verdict examples/judge/verdicts/forged-pass.json
```

The good fixture exits **0**. The forged PASS exits **1** because the runner
recomputes the score and checks each required dimension. In a real evaluation,
use an independent judge and retain its input, model identity, and raw verdict.
The runner prepares prompts and validates verdicts; it does not invoke the judge
or authenticate the origin of a supplied verdict.

These examples demonstrate evaluator behavior. They are not a model benchmark,
a security certification, or evidence of adoption by other maintainers.
