# Phase 3 — whole-video editorial and mechanical review

Read the COMPLETE original script from materialized B1.1 group texts
and the jointly proposed beat text AND visual decisions. Python reports
source and carrier violations while preserving the proposed visual intent.
Evaluate the chronological continuity
of every live sequence as an actual edited YouTube film: each action should
be owned by its real actor, consecutive stills should preserve the same
character and object state where required, and each reveal should be
visible and understandable. Evaluate continuous moving B-roll for actions and meaningful temporal change,
static inserts/grids for information that must be inspected, recurring avatar
presence for communication with the viewer, and source-anchored overlays
for meaningful headlines, claims, contrasts and reveals. Inspect EVERY beat's owner, observable change, communication purpose and
visual carrier; report when moving action becomes static images, when a
human-address moment loses the speaker, or when presenter footage replaces
a narrated event whose action should be visible. Inspect EVERY connected sequence for shared reference-frame identity,
including video-start frames, still inserts and returns after avatar
interludes. Assess still-grid suitability separately from continuity
across carrier types. Inspect editorially decisive spoken headlines,
warnings, contrasts and verdicts for **actual additive** typography only
when an on-screen graphic contributes to the final edit. Report the concrete missing
visual job, continuity requirement or overlay purpose under the relevant
beat_id; a general favorable assessment does not resolve an individual
omission. Review the actual video as one professional
YouTube story: hook strength, return hooks, narrative progression, cadence,
clarity/comprehension, dramatic changes, final payoff, natural avatar/visual
handoffs, causal and human continuity, specificity, purposeful B-roll, scene
feasibility, grid continuity, screen/document bans and Haiku overlay utility.

Python has ALREADY checked the combined audiovisual plan with
deterministic rules and passes concrete violations. When cut materialization
fails, evaluate the source and its already-proposed visuals TOGETHER rather
than requesting an independent text-only segmentation. There are NO tools to call:
the Python controller evaluates the full candidate exactly once before your
review. Do not ask Python to re-echo a giant plan. Do not confuse shot count
with narrative beat count: multiple images in media_image create additional
subshots, but not additional beats. There is no fixed rhythm target or
acceptable beats-per-minute quota. Respect the proven inherited B1.2
atomicity, coherence and viewing-cadence standards.

## Mandatory character-run and CTA audit

Independently inspect the reported audio/avatar runs across every adjacent
group boundary. A maximal uninterrupted audio=true run may total no more
than 120 characters after stripping each beat's text. Treat any reported
overage, including a one-character overage, as a required correction; do not
leave it for delivery or rely on the validator alone. Identify the smallest
meaningful source-faithful visual interruption that restores the cap, then
recount the complete run.

Separately scan the complete original script and proposed beats for every
explicit ebook, QR, offer or CTA mention. Reconcile each with the protected
CTA group/range and verify its authentic classification, group annotation,
and required ebook reference metadata. Inspect each spoken CTA beat and
generated CTA asset individually, including carrier/reference compliance,
the authentic cover input, and compositor-owned QR/website behavior. Any
unprotected CTA mention or missing required reference is an actionable
finding, even when the rest of its group appears valid. Apply
`rules/professional_cta.md`; preserve frozen CTA text, order and boundaries.

## Mandatory audit of the real visual storytelling

Review each carrier **against the alternative picture that would best serve
its narration**, without assuming that previously selected `avatar` footage
has an editorial justification merely because the original source starts as
a speaking presenter. Inspect every beat and its surrounding visual
sequence, including consecutive `avatar_media` decisions.

For each moment, verify these positive editorial outcomes:

1. The image communicates the moment's actual narrative job — visible
   event, relationship, explanation, evidence, contrast, reaction,
   interpretation, reveal or purposeful direct audience address.
2. The represented person or object owns the described action, possession,
   experience or consequence. The speaking narrator is distinguished from
   the story participant according to the source context.
3. The visual makes the source-supported meaning perceptible with concrete
   subjects, relevant details, comprehensible framing and appropriate
   prominence, rather than treating a related object as sufficient.
4. Connected moving shots depict actions and physical-state changes over
   time with preserved identity, space, causal order and reveal timing.
   Still-image grids support information-rich static continuity rather than
   replacing the motion that makes an event meaningful.
5. The recognisable speaking presenter returns naturally for direct address,
   interpretation, audience guidance, personal judgement, warning and human
   connection. Check the contribution of each return *and* detect extended
   B-roll runs that erase the presenter from audience-facing communication.
   `avatar_media` must justify its simultaneous layout.
6. The edit varies meaningfully between human connection, moving events and
   informative stable details. Inspect the actual visual experience across
   beats and group boundaries, rather than relying on carrier counts.
7. Only deliberately added postproduction elements receive editor notes.
   Inspect each beat-level `editor_notes` string as an actual
   imperative edit operation: a specific addition, content and placement
   where needed, with a valid trigger for timed effects. Flag notes that
   merely check generated imagery, request a review, assert visibility,
   describe brand authenticity or re-state image-generation requirements.
   Recognize protected code-owned CTA graphics as outside these notes.
8. Treat every related video starting frame and static detail as one
   chronological **editorial reference sequence**, even when the avatar
   interrupts the scene. Verify subject, prop and state continuity after
   each interruption. Distinguish this scene-level sequence from current
   physical grids, which only encompass contiguous static-image beats;
   do not report a mixed-carrier grid as technically generated when the
   current transport cannot construct it.
9. Read every generated prompt as a shot brief: check that it specifies the
   relevant subject/continuity details, necessary objects and state, a
   deliberate camera angle and framing, and a legible action or visual
   state. For each numbered grid, check that each successive prompt preserves
   the previous scene's relevant facts and precisely directs its next visual
   development or camera view. Return beat-specific findings for vague or
   discontinuous prompts, not a general request to "add detail".

Raise a `CreativeReview` issue for a **specific observed failure** even when
no Python violation exists. Specify the affected beat or connected sequence,
its missing visual contribution and the corrective visual outcome. Judge
selection, proportion and variety editorially; numeric avatar limits are
technical ceilings, not proof that the chosen coverage is entertaining.

## Editorial audit of block-sensitive YouTube shot rhythm

Assess the actual shot sequence **per original B1.1 group type and across
group boundaries**. A beat is one planned editing shot, while several
`media_image` stills inside one beat add timed visual subshots. A
continuing original base-avatar picture across consecutive `avatar`
beats is still one visual presentation. Judge visual activity, useful
progression and intelligibility together, not beat counts in isolation.

- For `intro`, inspect the speed with which the viewer receives an
  interesting, understandable visual hook and a clear narrative promise.
- For `development`, inspect purposeful variation as cases, steps,
  examples and causal explanations unfold; allow comparisons time to read.
- For `pivot`, inspect whether the new direction feels visually distinct
  and receives adequate orientation.
- For `correction`, inspect whether the previous understanding, new
  evidence and revised interpretation are visually distinguishable.
- For `climax`, inspect preparation, escalation and the visual emphasis
  given to the decisive payoff; a sustained revealing shot can be apt.
- For `close`, inspect a lucid landing of the final message and its
  surrounding image choices, honoring the existing last-beat contract.

If the supplied Python report includes `rhythm_map`, interpret
`basis=script_characters_not_seconds` literally: its source positions
estimate relative placement in narration, not playback duration.
`between_beat_changes` counts candidate visual changes across beats
except adjacent uninterrupted base-avatar beats;
`within_beat_changes` counts additional still-image subshots.
`rhythm_map.beats[].image_count` shows available extra views, not extra
narrative beats. Inspect the actual candidate visuals and source order
to judge WHETHER the changes are useful: a large count cannot prove
engagement, and a small count cannot by itself prove a pacing failure.
These values are observations of the already proposed plan and cannot
be treated as a target that preceded phase 2 or as measured STT seconds.

For a genuinely weak passage, report its **specific editorial issue**:
the beat or connected sequence involved, the mismatch between its
narrative job and its current visual pacing, and a permitted concrete
correction (a moving shot for unfolding action, a static evidence/detail
view, a renewed presenter-facing moment, or a source-boundary adjustment
where the visual job genuinely changes). Review
the entire arc without turning qualitative tendencies into rigid quotas.

CTA promo words, their placement in the original source, and any protected
ebook-reference visual beats are frozen. Treat such source order as immutable;
raise any visual transition concern against an editable adjacent beat or
record it in the assessment without requesting prohibited CTA changes.
Only report an issue when an actual permitted visual choice can address it.

Return a CreativeReview: brief whole_video_assessment and issues[] with
beat_id (or null for a global issue), concrete problem and specific required
correction. Omit speculative or cosmetic changes; focus on real editorial
deficiencies. This response does NOT repeat the plan or source text.


## Grid continuity across carriers

Check whether each continuing scene has been given one coherent numbered grid across all of its generated assets. A valid grid may span `media_image`, `media_video` starting frames and `avatar_media` support stills, even when those beats are not adjacent and even when `avatar` beats appear between them. Flag any plan that breaks such a scene into separate grids without a genuine scene change.


Also verify that grid naming is globally coherent: the first logical grid in the
video is `A`, the next `B`, and so on; each asset increments within its grid
(`A1`, `A2`...). Flag any one-asset "grid" because a grid must contain at
least two generated assets.
