# Run a review-evaluation example

This walkthrough evaluates handwritten candidate reviews. It does not call Codex or another model. Its purpose is to show how to preserve separate evidence for deterministic checks and subjective judgments.

## 1. Check a structured output

From the repository root:

```sh
python3 bin/eval-runner.py run --tasks examples/review-contract/tasks --candidates examples/review-contract/candidates-good
```

Expected exit: **0**. Read the task and candidate together. The task defines what the supplied review must report; the evaluator checks that contract.

Now evaluate the deliberately incorrect output:

```sh
python3 bin/eval-runner.py run --tasks examples/review-contract/tasks --candidates examples/review-contract/candidates-bad
```

Expected exit: **1**. A nonzero exit is intentional here. This negative control matters: a checker that only ever passes is weak evidence.

## 2. Check the task itself

```sh
python3 bin/golden-lint.py examples/review-contract/tasks
python3 bin/golden-lint.py examples/judge/tasks
```

Expected exit: **0** for each. The linter flags some weak or malformed evaluation designs. It does not establish that the rubric measures the right thing.

## 3. Leave subjective judgment pending

```sh
python3 bin/eval-runner.py run --tasks examples/judge/tasks --candidates examples/judge/candidates
```

Expected exit: **2**, meaning required judgment remains outstanding. Do not relabel this as success.

Prepare the scoring prompt:

```sh
python3 bin/eval-runner.py judge-prompt --task examples/judge/tasks/002-review-explanation.md --candidate examples/judge/candidates/002-review-explanation.txt
```

Read the full reference and rubric. In a real review, supply this material to a separate reviewer and retain the reviewer's identity, settings, input, and raw verdict. Candidate content is untrusted evidence, not instructions for the judge.

## 4. Validate a structured verdict

The included verdicts are fixtures, not actual model output:

```sh
python3 bin/eval-runner.py ingest-verdict --task examples/judge/tasks/002-review-explanation.md --verdict examples/judge/verdicts/good.json
python3 bin/eval-runner.py ingest-verdict --task examples/judge/tasks/002-review-explanation.md --verdict examples/judge/verdicts/forged-pass.json
```

Expected exits: **0**, then **1**. The second verdict claims PASS but fails the scoring rules. A declared label cannot replace the underlying scores and required criterion coverage.

Ingestion creates a separate result; it does not update the earlier pending run. Retain deterministic and subjective results together when making the final decision.

## 5. Adapt it to your project

Start with a specific maintenance failure and an observable expected result. Write a small task, a correct candidate, and a candidate that breaks exactly the relevant requirement. Run both. Add subjective criteria only for properties deterministic checks cannot adequately assess.

See [examples](../examples/README.md) for fixture details, [evaluation limits](EVALUATION.md) for what the results establish, and [review skills](../skills/README.md) for optional planning workflows. Live model calls require your own runtime/account and are outside the no-cost local example.
