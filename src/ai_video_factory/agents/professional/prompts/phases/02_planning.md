# Phase 2 — ONE global beat + visual direction response

Decide the narrative segmentation and visual realization of the COMPLETE
original script TOGETHER in this SINGLE creative response. Read the entire
materialized B1.1 groups and narrative core before choosing any boundaries.

Each beat is an inseparable editorial decision: its semantic source ending,
beat_type, observable on-screen action, real actor, visual carrier, prompt
assets, continuity/grid strategy and source-anchored editor notes. The same
director determines its natural YouTube pacing and its cinematographic
purpose simultaneously. There is NO separate beat-only model step and
NO separate visual-only model step.

Apply all loaded original B1.2 atomicity/semantic-boundary/cadence criteria
and hard <=100 stripped characters, <=40 words limits. Apply all loaded B2,
B2.1, real presenter identity, coherent video-motion, CTA/ebook, screen and
document, image-grid and Haiku rules to those SAME beats before responding.
The choice of cut must support the photographed action, continuity and
reveal; the choice of image must support its exact source narration.
No forced shot count or beats-per-minute rate.

Return a strict WholeVideoDirection object containing every B1.1 block in
original order, each with its COMPLETE ordered list of DirectedBeat objects.
A DirectedBeat includes end_anchor (a short exact phrase ending that beat
in the source), occurrence (1-based number after the previous beat),
beat_id in B1.2 sequence, beat_type, visual_type, audio, reference,
prompt, video_prompt, image_prompts, generation_mode, editor_notes,
cta_group_id and cta_reference_sources. All schema fields are required;
nullable ones must be explicitly null when inapplicable.

Never regenerate beat.text. Python materializes text literally using the
source anchors AFTER receiving your COMPLETE beat+visual plan and validates
the entire audiovisual result. Source cuts and visual choices must form a
single coherent narrative and stage sequence before the first submission.
If any detail is wrong, one whole-video correction phase will request
focused replacements carrying revised cuts AND revised visuals together.

The whole plan is one phase output. No intermediate narrated-beat output
can be considered a successful result without its own visual choices.
