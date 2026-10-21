# Run a small maintainer pilot

This is a proposed evaluation protocol, not a report of completed external use.

## Choose an authorized change

Use a public change you maintain or have permission to evaluate. Pick one small change with a reproducible failure and one clean control. Remove personal information and credentials. Record the repository commit, reproduction command, expected outcome, and permitted publication scope.

## Establish the answer before running a model

Have a human maintainer identify the relevant defect, its impact, and the evidence needed to confirm it. Define what counts as a matched finding and what counts as a false positive. Keep the expected answer separate from the review prompt when measuring discovery.

## Run and retain evidence

Record the model identifier, date, prompt, relevant settings, raw review, runtime test results, and provider-reported usage. Use the evaluation runner to check the supplied review's output contract. Have a separate reviewer assess substantive correctness; do not infer discovery from a fixture containing the expected finding.

## Report the small result honestly

Report missed defects and unsupported findings alongside correct ones. Separate provider-reported cost from estimates and wall-clock time from saved human effort. A two-change pilot is a workflow demonstration, not a general accuracy estimate. Never treat pilot participation as endorsement without consent.

## Turn learning into a contribution

Submit a minimal authorized regression case and a short account of what was difficult to use. Keep a correct and incorrect candidate together. An issue or pull request should include the exact reproduction and the project's relevant version.

The first useful milestone is another maintainer reproducing a result without help from the author. That milestone has not been claimed as completed by this release.
