# Detector evaluation against synthetic ground truth

`money-trail evaluate --detection-config FILE --namespace NS --ground-truth FILE` runs a bounded dataset-wide detection and
then compares it with the labels. It reads the graph and the detection configuration for detection, and reads the label file
only afterwards (a test asserts the order). Nothing is written to the graph.

## Separation of labels from investigation

* The investigation modules (`investigation/`) never import `money_trail.evaluation`, and their source never mentions the
  ground-truth file or `pattern_id` (both checked by unit tests that parse the source). The evaluation package imports the
  investigation package, never the reverse.
* Labels are never stored on any node, never written into findings or evidence, never part of a detection's inputs, and
  the manifest and ingest configuration do not contain them (Phase 2 tests). Phase 3 tests persist findings and then assert that
  no label id appears in any graph string value or in investigation output, that only schema properties exist on every node,
  and that detection works unchanged on new seeds and under renamed namespaces (no coupling to generated identifiers).
* The `evaluation.match` thresholds live in the detection configuration but are matching criteria, not labels, and are excluded
  from every rule version.

## Ground-truth format (version 1, produced by `money-trail generate`)

`{"format_version": 1, "namespace": ..., "patterns": [{"pattern_id", "type", "account_ids", "transaction_ids", "roles"}, ...]}`.
Types are `circular`, `pass_through`, `fan_in`, `fan_out`; identifiers are the canonical namespaced ids stored in the graph. The loader
rejects other versions, a namespace different from the evaluated one, duplicate ids, unknown types and empty lists.

## Matching (per pattern type, deterministic)

A detection D and a labelled instance G are eligible when they share a transaction and
`|D and G| * 100 >= min_gt_coverage_percent * |G|` (the detection covers enough of the instance) and
`|D and G| * 100 >= min_detection_purity_percent * |D|` (enough of the detection belongs to the instance), in integers.
Eligible pairs are assigned greedily, best first, ordered by larger overlap, then smaller `|D| + |G|`, then detection id, then
pattern id. Each detection and each instance is used once, so one detection can never count as a match for several instances, and
two detections of one instance give one true positive and one false positive. Greedy assignment is deterministic but is not
guaranteed to maximize the number of matches in contrived overlap structures. The example uses 100 % / 100 %, i.e. exact
transaction-set equality, chosen before any run.

Unmatched detections are false positives; unmatched labelled instances are false negatives. Labelled instances whose transactions are not
all in the graph are excluded and counted (`excluded_instances`). Detectors disabled in the configuration are reported as not evaluated.

## Metrics (exact fractions)

* precision = TP / (TP + FP); recall = TP / (TP + FN); F1 = 2PR / (P + R).
* Undefined values are reported as `null`, never 0 or 1: precision when nothing was detected, recall when nothing is labelled, F1 when either
  is undefined; F1 is 0 when both are defined and P + R = 0.
* Account coverage and transaction coverage = labelled accounts (transactions) of the type that appear in any detection of the type, divided by
  all labelled accounts (transactions) of the type.
* Overall figures are micro-averages over the four types. Reported counts are unreduced (`2/2`, not `1/1`).
* Each type also reports `search_truncated`; the report counts truncated searches, and the command exits 4 when the detection was incomplete.
* False-positive examples are classified as `partial_overlap_with_same_type_instance`, `overlaps_instance_of_other_type` or
  `no_overlap_with_any_labelled_instance`; false-negative examples list the best overlapping detection.

## Measured results (actual runs; configuration `examples/detection.example.json`, unchanged between runs)

How to read them. The synthetic generator builds patterns with known parameters, and the example thresholds were set from those documented
generator parameters before any evaluation (for example circular retention 9000..10000 bps against a generated 9900, fan windows of one day
against generated spreads up to one day). The benchmark is therefore in-distribution: high recall is expected and says little about real data.
Seeds used during development and in tests were not used for the figures below. No detection threshold was changed after seeing any
evaluation result. (The only edit to the example file after the first evaluation was `query_timeout_ms` from 60000 to 20000, an operational
setting that the new rule "timeout below the socket timeout" required.)

Standard datasets (generator example parameters: 60 ordinary accounts, 300 ordinary transactions, 30 days, 2 instances per type, fresh seeds
424242, 515151, 626262, 818181), pooled over the four datasets:

| Type | Labelled | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| circular | 8 | 8 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| fan_in | 8 | 8 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| fan_out | 8 | 8 | 1 | 0 | 0.889 | 1.000 | 0.941 |
| pass_through | 8 | 8 | 16 | 0 | 0.333 | 1.000 | 0.500 |
| micro | 32 | 32 | 17 | 0 | 0.653 | 1.000 | 0.790 |

* All 16 pass-through false positives are strict sub-chains of labelled circular instances: a time-ordered cycle contains chains that satisfy the
  pass-through rule, and only the cycle is labelled. This is a structural overlap between two of the rules, not noise.
* The one fan-out false positive is a window that also contains an extra ordinary transfer from the distributor.

Noisy dataset (same 60 accounts but 1,800 ordinary transactions, seed 737373; configuration not tuned for it):

| Type | Labelled | Detected | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|---|---|
| circular | 2 | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| fan_in | 2 | 32 | 2 | 30 | 0 | 0.062 | 1.000 |
| fan_out | 2 | 34 | 2 | 32 | 0 | 0.059 | 1.000 |
| pass_through | 2 | 16 | 2 | 14 | 0 | 0.125 | 1.000 |
| micro | 8 | 84 | 8 | 76 | 0 | 0.095 | 1.000 |

Fan and chain structures arise by chance when ordinary activity is dense, so with the same thresholds precision collapses while the labelled
instances are still found. Thresholds must be set against the data's own base rates; the evaluation shows how much they matter.

Larger dataset (seed 909090: 3,000 ordinary accounts, 60,000 ordinary transactions, 90 days, 50 instances per type; 61,013 transactions, 200
labelled instances, ingest 8.7 s, full detection 10.1 s, complete, nothing truncated):

| Type | Labelled | Detected | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|
| circular | 50 | 50 | 50 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| fan_in | 50 | 55 | 49 | 6 | 1 | 0.891 | 0.980 | 0.933 |
| fan_out | 50 | 55 | 48 | 7 | 2 | 0.873 | 0.960 | 0.914 |
| pass_through | 50 | 199 | 50 | 149 | 0 | 0.251 | 1.000 | 0.402 |
| micro | 200 | 359 | 197 | 162 | 3 | 0.549 | 0.985 | 0.705 |

All three fan false negatives are labelled instances contained in a larger detection with exactly one extra ordinary transfer inside the window: under
100 % purity matching they count as misses (and the larger detection as a false positive), although discovery found every labelled transaction.

Threshold sensitivity, observed by accident: on an earlier performance dataset whose fan patterns had 3 to 4 counterparties, the example rule (minimum 5)
found 0 of 50 fan-in and 0 of 50 fan-out instances. Recall is a function of the configured thresholds as much as of the detector.

## Limitations

The data are synthetic and patterns are constructed with the same structure the detectors look for; these figures do not estimate real-world accuracy.
Random activity can produce incidental structures, counted as false positives. Metrics hold only for the configuration used. Exact matching is strict
by design; looser `evaluation.match` values change TP/FP/FN and should be chosen before, not after, looking at results. Tuning detector thresholds on
these labels and then reporting the same labels as an independent benchmark would be circular, and the repository does not do it.
