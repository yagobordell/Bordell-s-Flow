# segmentation_and_pacing.md — Agent B: Joint Beat Segmentation and Audiovisual Rhythm

**Status:** Draft learnable agent instruction; awaiting design approval.  
**Mode:** Production; may be revised only through authorized calibration and evaluation.  
**Scope:** Professional whole-script planning.  
**Authority:** Creative guidance subordinate to `core.md`, `workflow.md`, and runtime-enforced `CONTRACTS_AND_VALIDATION.md`.  
**Companion documents:** `creative_principles.md`, `narrative_continuity.md`, `visual_direction.md`, and `references/image_prompting.md`.

> **Learning ownership:** This file owns **beat-boundary and pacing implementation** (including motivated in-sentence suspense cuts). `creative_principles.md` owns the underlying universal editorial value; `narrative_continuity.md` owns persistent characters and A/B image chains. Do not replicate new generic rules in multiple learnable files.


## 1. The purpose of a beat

A beat is a **deliberate unit of narrated time and audiovisual attention**. It is not necessarily a sentence, an independent grammatical statement, or a separate location. Its boundary determines what the audience encounters, when that information arrives, and what visual treatment carries it.

**Choose each beat boundary and its visual treatment together.** Decide what the audience should register or feel at that exact moment, which part of the authoritative narration should occupy the beat, and what picture will make that moment effective. Do not first partition text by punctuation and only afterward try to fit pictures to arbitrary slices. Likewise, do not split a cohesive idea merely because several image prompts could be invented for it.

The aim is **expressive pacing with faithful narration**: meaningful action, intelligible explanation, controlled suspense, emotional contrast, and intentional holds. There is no target number of beats, default duration, fixed introduction cadence, or universal average length.

## 2. Understand the whole video before selecting cuts

Consult the full B1.1 script and its narrative structure before finalizing local cuts. Determine:

- The video's central communicative goal and the role of each major passage.
- What the viewer knows, suspects, misunderstands, or is still waiting to learn at each point.
- Which facts or visible outcomes are intentionally revealed later.
- Where the narration changes actor, action, cause, consequence, perspective, time, location, argument, or emotional pressure.
- Where the viewer needs a clear new picture, and where a continuing picture will communicate better.
- Whether a sequence returns to an earlier character, object, scene, or visual thread after intervening material.

Do not assume every script has a thriller-style hook, chronological events, a single protagonist, or a steadily increasing tension curve. The right pattern depends on the actual narration. Use B1.1's `type`, `emotional_entry`, `emotional_exit`, and `narrative_core` as context, not as commands to force a particular pacing formula.

Working groups may make long scripts manageable, but they are **not editorial walls**. Review preceding and later context as needed. Preserve the original B1.1 block boundaries even when one dramatic or visual sequence spans several blocks.

## 3. Decide the cut through its effect on the viewer

Before establishing a boundary, identify the **editorial change** it will create. Useful motives include:

| Motive | What the cut can accomplish |
|---|---|
| New physical action | Give an independently meaningful action its own visible moment. |
| Change of subject or recipient | Redirect attention to the person or object that now matters. |
| Cause to consequence | Let the viewer recognize the causal relationship and its result. |
| Progressive discovery | Reveal information in steps rather than showing the answer prematurely. |
| Anticipation or tension | Make the viewer wait briefly for a meaningful missing detail. |
| Surprise or reversal | Make a new fact reframe what the viewer believed a moment earlier. |
| Contrast or analogy | Make a relevant difference or parallel comprehensible. |
| Explanatory transition | Allow a mechanism or complex relationship to become understandable. |
| Emotional reaction | Create space for an important reaction or changed understanding. |
| Direct address | Return to the narrator when human emphasis or connection strengthens the message. |
| Deliberate hold | Keep one visual in place when another cut would weaken clarity or emotional impact. |

The test is not whether two fragments *could* be illustrated independently. Ask whether the proposed boundary **earns its place** in the viewing experience. An action-rich passage may need multiple closely spaced visual beats; a dense explanation may need fewer and more stable treatments.

A visual beat does not always require an unrelated image or new setting. A change in framing, focal subject, physical state, or visible detail can advance the same scene. Conversely, the same image may remain useful across closely related material when a new visual reset would be distracting—subject to the actual output/timeline contracts.

## 4. Intentional cuts inside sentences are valid

A beat **does not have to be independently grammatical** when an intentional boundary creates anticipation, delayed disclosure, emphasis, or an effective payoff. A fragment can work because the viewer understands that the next fragment completes the thought.

A justified sentence-spanning cut should meet the following editorial conditions:

1. The meaning of the source remains recoverable in its original order.
2. The incomplete fragment creates a purposeful expectation, contrast, or moment of attention.
3. The following beat supplies a meaningful continuation or payoff rather than merely repairing an accidental fragment.
4. The visual treatment supports the staged revelation instead of leaking the later answer early.
5. The combined sequence still has natural spoken cadence and does not become a string of arbitrary fragments.

Do **not** apply an absolute rule that every beat must be a standalone sentence or independently intelligible clause. Equally, do not make mid-sentence cutting the default. An incomplete phrase that creates no useful anticipation is usually better kept with its completion.

A short payoff such as **“It matches.”** can deserve its own beat even though it is only two words, because the entire setup has made the result important. The legacy prompt's general caution against microbeats is **editorial advice, not a reason to discard a genuinely motivated payoff**. Hard source, character, and word-count validations still apply.

## 5. Approved suspense example: the incoming call

The following opening is an **approved example of escalating tension and staged revelation**, drawn from a single source passage. The seven visible lines illustrate desired beat boundaries; they are not permission to replace source spaces with newline characters in the serialized `beat.text` fields:

```text
Your phone rings.
You look at the screen.
Chase. The bank’s logo.
And the number is the same one
printed on the back of your debit card.
You even pick up the card to check.
It matches.
```

### Why these cuts work

| Moment | Editorial effect | Visual progression |
|---|---|---|
| `Your phone rings.` | Introduce an interruption and a question: who is calling? | Establish the same phone and immediate situation. |
| `You look at the screen.` | Move attention to the evidence the viewer expects to trust. | Maintain the same person and phone; reveal their reaction/framing. |
| `Chase. The bank’s logo.` | Add a familiar authority cue before the underlying suspicion is resolved. | Focus on the phone as the source of the perceived signal; obey the screen-content prompting rule. |
| `And the number is the same one` | Withhold the comparison's decisive referent for a moment. | Hold the examination; do not reveal the final match yet. |
| `printed on the back of your debit card.` | Introduce a second physical object and complete the comparison. | Bring the same person's debit card into the existing situation. |
| `You even pick up the card to check.` | Intensify credibility through the person's deliberate verification action. | Show the physical check while retaining identity and prop continuity. |
| `It matches.` | Deliver the short, decisive payoff; the apparent proof is the trap. | Emphasize the moment of certainty without introducing an unrelated image. |

These are seven **distinct visual/narrative moments in one continuous setup**. Plan them as one logical visual continuity sequence when using generated assets (for example `A1.` through `A7.`), so the phone, card, person, setting, and evolving action remain coherent. The runtime may generate the sequence across several **physical 2–4-panel grids**, with valid reference chaining; do **not** require a seven-panel physical grid.

The first-person identity of the later named character in the broader script must not be imposed on this generic second-person opening unless the source establishes that relation. Nor should unrelated identity attributes be invented simply to make the scene vivid.

**Special image-prompting constraint:** The narration mentions a bank name, logo, and phone number, but Bordell's protected **screen/document rule** still applies. For a phone-screen image, describe the physical phone, its framing and the person's interaction, without prescribing visible screen content in the body of the image prompt. Put the relevant narrative situation in the required final `(For context: ...)` note. Source facts remain in the narration; a visual prompt must not violate the generator-facing screen rule to reproduce them.

### What this example does **not** teach

It does **not** establish a required seven-beat opening, a rule to cut every sentence, a fixed number of characters per shot, or an obligation to make every introduction tense. It demonstrates a conditional principle: **when a passage builds credibility or uncertainty through successive pieces of evidence, cut and direct the visuals so the viewer discovers those pieces in the intended order**.

## 6. Other pacing modes: apply the same judgment differently

### Explanation or instruction

When the viewer must understand a mechanism, a procedure, or a chain of reasoning, prioritize clear steps and sufficient visual time. Separate genuine successive actions, causes, and observable results. Keep modifiers or supporting details together when splitting them would obscure the governing idea. A series of related examples or a compact list may fit one coherent beat when its members form a single message.

Do not fragment a dense explanation into disconnected microbeats merely to create activity. Instead, use a precise subject, a useful composition, a meaningful change of view, or a visual subshot when justified by the allowed image contracts.

### Contrast, analogy, and argument

Decide whether both sides must be understood **together** or whether a staged reveal is more effective. A short contrast may form one beat when its meaning depends on simultaneous comparison. A suspenseful reversal may need separate beats when timing the second side changes the viewer's interpretation.

### Reflective or emotional passage

Give an important thought or emotional consequence sufficient space. A long hold or avatar address may carry more weight than several generated shots. Do not mistake stillness for a lack of direction.

### Recurring narrative thread

When a script leaves one situation, explains a mechanism, and later returns to the same character or event, preserve the corresponding visual continuity without forcing every intervening beat into that same sequence. A logical chain can be `A1.`, `A2.`, then unrelated `B1.`, `B2.`, then `A3.` in a later B1.1 block.

## 7. Segment and select visuals in one planning action

For each candidate stretch of source narration:

1. Identify the current **narrative impact** and desired viewer experience in whole-script context.
2. Consider whether a meaningful cut is warranted **at a particular source boundary**; punctuation is evidence, not authority.
3. Identify the best visual carrier and its narrative focus for each prospective beat. The original speaker is valuable when direct address helps; generated imagery is preferable when it makes the action or explanation clearer.
4. Consider whether adjacent or distant beats belong to one logical continuity sequence. Maintain coherent actors, props, locations, and chronology.
5. Check whether the proposed visual is a photographable still, a feasible video-start frame plus motion, or a useful supporting image alongside the avatar.
6. Confirm that the combination serves the emotional/explanatory rhythm, without premature reveals, redundant images, or pointless microcuts.
7. Save the **joint beat-and-visual decision** through the authorized plan tool; do not produce separate B1.2 and B2 creative outputs.

This is an editorial decision process, not a requirement to publish reasoning notes. Do not add `audience_intent`, `cut_intent`, cut explanations, or continuity metadata as new mandatory fields on public beats. The final artifact retains the functional shape of the existing professional `visual_plan.json`.

## 8. Immutable text and hard segmentation invariants

The original script and B1.1 materialized blocks are **authoritative**. The agent may choose cuts only **within** each original B1.1 block.

- Each `beat.text` MUST equal an exact contiguous slice of its corresponding B1.1 block text, including original punctuation and whitespace.
- Concatenating the ordered beat text slices for a block MUST reconstruct the complete block **exactly**. Never rewrite, omit, duplicate, translate, normalize, or move narration between blocks.
- Existing `beat_id` naming remains block-local and sequential: `1A`, `1B`, and so on. These are distinct from global image-sequence labels `A1.`, `A2.`, etc.
- Each beat MUST contain **no more than 100 decoded characters after `.strip()`** and **no more than 40 words** under the current validator's rule.
- Beat types MUST use the existing `BeatType` vocabulary.
- There is no fixed beat count per block and no imposed target average length.

**Whitespace at a cut matters:** the line breaks shown in editorial examples denote cut positions; they are not literal replacements for the spaces in a one-paragraph source. Use source offsets or exact source slices when saving. The runtime must verify fidelity independently. This matters especially for intra-sentence cuts.

The legacy B1.2 instruction to prefer grammatical completeness has been superseded **as an unconditional editorial rule** by the approved allowance for emotionally motivated phrase-level cuts. This does **not** supersede the protected data-integrity, character, word, or source-boundary contracts.

## 9. Pacing and visual constraints interact

### Speaking-avatar runs

In the professional workflow, consecutive `audio=true` beats across **block boundaries** count toward the hard maximum of **120 characters**, using the existing sum of `len(beat.text.strip())`. The agent may use a narratively justified `audio=false` B-roll beat to break an overlong run, but must not insert generic footage solely to game the number. Protected closing-avatar behavior remains mandatory.

A cut into several shorter `audio=true` beats does **not** reset the speaking-avatar count. Choose carriers with the complete sequence in mind and execute `validate_plan` as needed.

### Long still-image beats and subshots

Under the existing professional contract, a non-CTA `media_image` beat of **70 or more source characters** is a deterministic candidate requiring **exactly two distinct still-image prompts within the same beat**, subject to the protected B2.1-derived rules. This is a visual subdivision, **not** another spoken beat or a new narration segment. Coordinate subshots with any logical image-sequence numbering and the runtime's grid planner.

Two images for a 70+ character beat do **not by themselves prove** that either image is visible during narration covering at most 70 characters. A true per-image coverage cap requires a separate verified text/audio-to-subshot allocation and is **not an approved hard contract yet**. Do not claim compliance with such a rule merely from the number of images.

### Grids and logical continuity

Use global identifiers such as `A1.`, `A2.`, `A3.` for image assets that need common identity or state continuity. The same logical sequence can cross blocks and resume after unrelated sequences. Labels count **image assets**, including eligible subshots, not words or beats.

The agent chooses continuity; the runtime decides physical grid packing, exact local panel numbering, reference chaining, and job feasibility. Do not force a long sequence into one grid. Respect carrier-format separation, CTA boundaries, reference ownership, and all current preflight validations.

### CTA and protected decisions

Ebook CTA detection and visual planning operate on **complete stable beats**. Before the final plan is frozen, coordinate cut changes with CTA association so promotional content is neither lost nor assigned to the wrong fragment. Once CTA beats are protected, do not alter them in an ordinary review pass. Preserve the closing anchor and downstream QR/composition compatibility.

## 10. Review the rhythm of the complete plan

After planning all groups, read the **entire resulting order** and review the experience from the first beat to the last. Do not assess pacing only within individual B1.1 blocks.

Check:

- Does the opening create the intended initial attention **for this particular script**, without manufacturing tension that is not present?
- Are actions separated where the viewer needs to see their sequence, and grouped where a single uninterrupted event is more coherent?
- Does any cut inside a sentence create a real expectation or emphasis, rather than an awkward dangling phrase?
- Does a visual reveal the answer too early, undermining a scripted payoff?
- Are high-intensity passages given meaningful escalation and subsequent room to absorb their consequences?
- Do educational passages offer enough visual continuity to understand causes and mechanisms?
- Are visual sequences consistent even if different sequences are interleaved or separated by many blocks?
- Do repeats strengthen recognition or produce unwanted stagnation? Are holds intentional rather than accidental?
- Is the avatar used for genuine connection or emphasis rather than as the core picture by inertia?
- Do technical caps, CTA ownership, source fidelity, and generated-image feasibility still hold after any edit?

Correct weak passages with the smallest coherent change; preserve decisions that already work. A local change may affect neighboring beat IDs, logical image numbering, CTA grouping, or avatar-run totals. Revisit dependent state and rerun the relevant Python checks. Only the controller's global acceptance gate may freeze the complete plan.

## 11. Autonomous tool use and calibration discipline

Use authorized tools **directly**: `read_script` and `read_group` for context, `read_project_context` for project-local continuity, `save_visual_plan` for atomic beat+visual edits, `manage_visual_sequence` for logical image chains, and `validate_plan` / `read_validation_report` to find and repair hard violations. Tool execution is real and does not require routine human approval. The controller still performs independent mandatory checks.

If a narrative decision is technically invalid, change the candidate plan, not a validator threshold. If a creative decision is poor, revise it using the whole script; do not pretend that passing Python validation proves audience impact. If a problem cannot be repaired within authorized boundaries and budgets, report the actual failure.

This document is **learnable**, but only in calibration mode. Feedback about one suspenseful bank-call opening is evidence for a broader conditional editing principle, not a command to reproduce those exact cuts or objects in every future video. A quiet or explanatory introduction should be allowed to remain quiet or explanatory. The authorized `learning_protocol.md` decides whether an observed case warrants a durable change.

**Completion standard:** one complete, source-faithful, creatively reviewed, validated audiovisual plan. No separate B1.2/B2/B2.1 model outputs, no mandatory per-beat reasoning fields, and no permanent instruction edits during production.