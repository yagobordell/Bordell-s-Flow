# Phase 4 prewarm stale queue summary

Salad queue `current_queue_length` can remain non-zero after all enumerable jobs are terminal. The controlled cleanup path already treats exhaustive job pagination as authoritative when it finds no `pending` or `running` jobs.

Optimized prewarm follows the same invariant:

- a non-zero summary is verified by exhaustive job enumeration before GPU allocation;
- if enumeration is complete and no active jobs exist, the reported length becomes the verified-empty baseline;
- any increase above that baseline during prewarm aborts before Phase 4 queue submission;
- an ambiguous or incomplete enumeration fails closed;
- no queue repair is performed.

This keeps the cold-start cost guard while avoiding false failures caused by a stale Salad queue summary.
