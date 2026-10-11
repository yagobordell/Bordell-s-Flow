# Group narrative method — B1.1, verbatim

## Narrative hierarchy and competence

A **beat** is a lower-level visual or narrative moment that B1.2 will recover later.

A **scene** develops a continuous dramatic or expository situation through one or more beats.

A **sequence** groups one or more scenes or beats around a local narrative task, question, development or payoff.

A **block** is an independently meaningful narrative movement composed of one or more sequences that jointly perform one distinct block-level function in the progression of the whole script. A block is larger than a sequence, but it does not need to encompass an entire act, chapter, list, tutorial or overarching thesis.

A block can contain several local questions, explanations, examples, consequences, scenes, time shifts or internal emotional turns only while those elements remain necessary parts of the same independently complete narrative contribution.

Do **not** merge independently complete submovements merely because they serve the same overall thesis, list, tutorial, argument or macro-topic. A numbered item, demonstration, warning, correction, comparison, reveal, payoff, application, case, or conclusion may form its own block when it completes a meaningful contribution before the narration moves to another contribution.

The number of blocks is determined by the architecture of the script. Blocks remain coarser than beats, scenes and sequences, but they must preserve independently complete submovements instead of collapsing multiple completed functions into a few oversized containers. A script may legitimately contain many blocks when it develops many distinct completed contributions.

A complete script may contain only one block only when no internal stretch completes an independently meaningful contribution before the script ends.

Local beat-scale, scene-scale or sequence-scale changes alone are not evidence of a block boundary; independently completed submovements are.

---

# PASS 0 — Whole-script calibration

Read the entire script from beginning to end without dividing it.

Before choosing any block boundary, determine `narrative_core` from the complete script.

## narrative_core

### central_question

The central narrative or explanatory question the whole script pursues.

### final_answer

The answer this script actually resolves by its end.

Preserve what the script actually resolves. An open thread, deferred answer or promise of continuation remains unresolved and must not be converted into a completed answer.

If the script ends without fully resolving the central question, `final_answer` must state that the script leaves it unresolved and may include only the partial answer the script actually establishes.

`central_question` and `final_answer` calibrate the scale of block division. Blocks are the distinct independently meaningful steps through which the script moves from the central question toward the final answer.

Only after `narrative_core` is established may you begin block segmentation.

---

# PASS 1 — Script → Blocks

## What makes one block

Each block makes one distinct, independently complete contribution to the path from `central_question` to `final_answer`.

Describe that contribution internally as one clause about what the block accomplishes for the script as a whole.

A block-level function is complete when the stretch has fulfilled the specific narrative job for which that submovement exists. Its contribution includes the material required for that job to become clear at block scale. Merely introducing, naming or partially developing that specific job does not complete it.

A block-level function is independent when the immediately following stretch is no longer needed to finish that same specific contribution.

A later block may depend on an earlier block for context, premise, causality or meaning. That dependence does not make it part of the earlier block. The distinction is whether the later material builds from a completed contribution or finishes the contribution already underway.

A valid block may contain several local tasks only when they are functionally interdependent and none of them has already completed a meaningful contribution that stands on its own in the script progression.

Do not use a shared umbrella purpose as sufficient reason to merge adjacent completed contributions. If one subsection has already delivered its own setup, development and payoff or its own distinct explanatory/use-case function, and the next subsection begins another such function, create a new block even when both support the same broader thesis.

A new block begins when the current contribution is complete and the narration starts another independently meaningful contribution to the whole-script progression.

Use the whole script to understand significance and context, but decide each boundary by whether a distinct block-level contribution has completed and another begins. Do not require a contribution to redirect the entire script before it can qualify as a block.

## Block boundary decision

Determine block boundaries top-down from the script's complete progression.

For every proposed boundary, identify internally:

1. what specific block-level contribution has been fully accomplished on the left;
2. what different independently meaningful contribution begins on the right;
3. whether the right-hand stretch develops that new contribution or is still required to complete the specific contribution active on the left.

A boundary is valid when the left-hand stretch has completed its block-level contribution and the right-hand stretch begins another independently meaningful contribution to the whole-script progression.

Two adjacent stretches belong to one block when the right-hand stretch is still needed to fulfill the specific contribution already underway on the left. Sharing the same overall subject, list, tutorial or thesis is not enough to merge them.

### Over-segmentation check

For every adjacent pair of candidate blocks:

1. identify the specific block-level contribution active in the left candidate;
2. ask whether that contribution is narratively fulfilled at the proposed boundary;
3. if the right candidate is still required to complete that same specific contribution, merge them;
4. if the left candidate has fulfilled its contribution, the right candidate may begin a new block even when both share the same topic, list, tutorial, thesis, context, premise, causality or meaning.

An internal change may be substantial and still belong inside one block when it finishes the same specific contribution. But if the left side has already completed a meaningful contribution and the right side begins another independently complete use, example, warning, correction, demonstration, reveal, comparison, payoff or conclusion, keep the boundary even when both belong to the same broader topic.

### Under-segmentation check

For every remaining candidate block:

1. describe internally its specific block-level contribution in the whole script;
2. examine its internal progression;
3. split it when one part has already completed a self-sufficient meaningful contribution and the following part begins another independently meaningful contribution rather than remaining necessary to complete the same specific contribution;
4. treat numbered/listed items, separate applications, demonstrations, warnings, corrections, reveals, comparisons and conclusions as strong split candidates when each performs its own complete narrative job;
5. verify that each resulting side remains a meaningful step in the whole-script progression when considered on its own.

The presence of internal beat, scene or sequence structure is expected and is not by itself evidence of a block boundary. The completion of an independently meaningful submovement is.

### Narrative grouping judgment

A block is a complete, independently meaningful macro-contribution to the
central question, with its own narrative entry, development and payoff.
Keep its necessary evidence, explanation, action, reaction and internal
micro-beats together until that contribution lands. A distinct completed
contribution merits a separate block even if both share topic, narrator,
person, place or larger thesis. Use the actual text and macro-function,
never target numbers of groups, beat count, or timing.

## Transition sentences and hinges

A sentence or clause that bridges two stretches belongs to the side whose macro-function it is performing.

Determine ownership from what the hinge does in the whole-script progression, not from a fixed linguistic form.

A hinge may complete the current block, open the next block, or remain inside one continuous block depending on its actual narrative function.

---

## Exact block spans

After narrative block boundaries are settled, identify each block with exact textual anchors in the authoritative script.

Each block emits:

- `span.first_words` — exact verbatim source words at the beginning of the block's substantive content;
- `span.last_words` — exact verbatim source words at the end of the block's substantive content.

Copy both anchors directly from `plain_script_for_recording`, character for character, preserving capitalization, punctuation and symbols. Do not paraphrase or normalize them.

Use enough contiguous source text for each anchor to resolve uniquely to the intended occurrence. Prefer a complete sentence when the boundary coincides with a sentence boundary and that sentence resolves uniquely. If the narratively correct boundary falls inside a sentence, anchor the exact clause or textual run that begins or ends at that boundary rather than moving the boundary to satisfy punctuation.

### Range resolution rule

The anchors locate the block boundaries without requiring separator whitespace to be embedded inside `first_words` or `last_words`.

Resolve exact block ranges as follows:

1. Block 1 starts at character 0 of `plain_script_for_recording`, including any leading whitespace before its `first_words`.
2. Every block after block 1 starts at the first character of its uniquely resolved `span.first_words`.
3. Every non-final block ends immediately before the first character of the next block's uniquely resolved `span.first_words`. Therefore separator whitespace between two substantive block contents belongs to the preceding block.
4. The final block ends at the final character of `plain_script_for_recording`, including any trailing whitespace after its `last_words`.
5. For every block, `span.last_words` must resolve inside that block's range and end at the block's final substantive source content; only separator/trailing whitespace may follow it before the resolved range ends.
6. `span.first_words` must identify the block's first substantive source content; for block 1, only leading whitespace may precede it inside the range.

The resolved ranges must tile `plain_script_for_recording` exactly once, in ascending `block_id` order, with no gap, overlap, duplication or reordering.

After choosing every anchor, verify that:

1. each anchor matches the script exactly;
2. each anchor resolves uniquely to the intended occurrence;
3. each `first_words` identifies the intended beginning of substantive block content;
4. each `last_words` identifies the intended final substantive block content;
5. all ranges resolved by the rule above tile the entire script exactly once.

Do not emit full block text, character offsets or any duplicate boundary representation. B1.2 resolves the exact block text from these spans plus the authoritative raw script before beat segmentation.

---

## Block fields

### block_id

Sequential integer from `1` in script order.

Every later layer references a block by this ID.

### type

Exactly one value describing what the block does to the script as a whole:

- `intro` — establishes the opening macro-movement and the initial terms from which the script will progress. Eligible only for the first block.
- `development` — advances the script's established line through a new independently meaningful contribution without fundamentally redirecting it.
- `pivot` — redirects the script's governing understanding, direction or stakes at macro level.
- `correction` — replaces, limits or overturns a macro-level understanding the script had established.
- `climax` — delivers the movement of greatest consequence, pressure, proof or culmination in the script's progression.
- `close` — performs a final macro-movement whose principal job is to resolve, synthesize or land the script. Eligible only for the final block.

These six labels are exhaustive. Never invent or substitute another block `type`.

Assign `type` only after block boundaries are settled.

Choose the type from the block's dominant macro-function. Edge position makes `intro` or `close` eligible but does not require either label.

A script consisting of one block receives the type that best describes that block's dominant whole-script function.

A local pivot, correction, reveal, reversal or climax inside a block remains part of that block when it serves the block's larger macro-function.

### emotional_entry / emotional_exit

These fields describe the viewer state at the exact opening and exact ending of the already-determined block.

For blocks after block 1, `emotional_entry` describes the state in which the preceding script leaves the viewer immediately before the block's first substantive content is processed.

For block 1, use only an initial viewer condition explicitly presupposed by the opening before its first substantive change. If the opening presupposes no specific viewer state, state neutrally that no specific viewer state is established before the opening. Do not infer one from content that has not yet been processed.

`emotional_exit` describes the state produced after the block's final substantive content has been processed.

Ground both states in the actual narration.

Prefer changes in expectation, understanding, uncertainty, interpretation, confidence or perceived stakes when those are what the text establishes. Use stronger affective states only when the narration supports them.

The two states may differ greatly, subtly or barely at all. Their distance describes the block; it does not determine whether the block exists.

Write the pair only after block segmentation is complete.

### span

`span` contains exact verbatim anchors for the block's start and end in `plain_script_for_recording`.

#### first_words

The exact contiguous source text beginning at the block's first character. Use enough text to resolve uniquely to the intended occurrence.

#### last_words

The exact contiguous source text ending at the block's final character. Use enough text to resolve uniquely to the intended occurrence.

The anchors identify the block range; they do not summarize it and must never be paraphrased.

---