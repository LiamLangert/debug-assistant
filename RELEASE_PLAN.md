# Release plan

## 1. Release candidate

BM25 on the traceback. This is the only complete row, so it is the model we would run.

Commit `3426c5a3f8101b443e6f5e12decf46764acb18e0`.

There is no saved model file and no pickle. The ranker is `scripts/baseline/run_baseline.py` with k1 1.5 and b 0.75. No LLM API.

Validation, 91 bugs: line Recall@5 7/91, file Recall@5 46/91. One run, spread 0. The log is `results/baseline_validation_hints.json`.

Classical (9/91 lines, commit `0c48deb`) and the deep row are preliminary. The deep 25.3% is function recall, not line recall. The decision rule does not use it.

## Before the final showcase

Complete the classical row and the deep row on the same line Recall@5: the Storyboard B slice, the median latency, and the test split.

Drop post-based line ranking. Those runs scored 0 to 2 of 91 lines. Traceback-only BM25 scored 7 of 91.

## The rest of the plan

Serving. Embedded in that local script. Not a separate service.

Stages. The three of us already saw the 91 validation bugs. On Nov 24 the class sees the demo, about 7 minutes, a few bugs on screen.

Promotion gate. Line Recall@5 at least 7/91 overall, tornado file recall not below 4/16, median latency at most about 5 seconds. The test split and the Storyboard B slice are not run, so this gate has not passed.

Rollback. Trigger is a failed gate, or a hint shown as strong that does not touch the fix. Liam checks out commit `3426c5a3f8101b443e6f5e12decf46764acb18e0` and reruns the baseline. The target is this same BM25, not an older LLM. The switch has not been timed.

What the developer is told. The list is hints, not a patch. The score is a match, not a probability. A weak flag means dismiss the list and search.

Risk. The project has no high or medium risk label, so there is no extra scoping rule.
