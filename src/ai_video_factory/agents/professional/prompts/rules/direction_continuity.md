# Professional YouTube visual direction — editorial subject and continuity

## Direction of a narrated film

The script is the presenter's voice-over. The picture track transforms that
narration into observable, intelligible audiovisual storytelling. A useful
shot gives the audience an experience the spoken words alone cannot: it
reveals what happens, shows how events unfold, establishes evidence, makes a
relationship perceptible, or restores human connection with the narrator.
Plan each shot in the context of the preceding and following images and the
current group's narrative purpose.

## Match the visual medium to the meaning

**Movement and temporal development belong to `media_video`.** When the
significance of the narration is an action, interaction, shifting expression,
physical response, behaviour, change of state or continuous event, depict
that evolution as a moving shot. Give the performer or affected participant
the visible action; show the development, rather than substitute a still
pose associated with it. Use feasible, source-faithful motion within one
uninterrupted clip, with the voice-over continuing above it.

**Stable information belongs to `media_image`.** When the viewer primarily
needs to examine an unmoving detail, evidence, legible distinction,
comparison, object, final state or spatial arrangement, compose a purposeful
still. Multiple still subshots may reveal complementary static views or
closer information within a beat, provided each contributes something
worth observing. A sequence of stills is not motion footage merely because
the depicted subject could move.

**Communication with the audience belongs to `avatar`.** The presenter
should recur as the recognizable human guide when speaking to the viewer,
introducing or interpreting a narrative turn, explaining a judgement,
delivering a warning, acknowledging an emotion, inviting a response or
landing a personal message. Keep the narrator on screen when being seen
strengthens that exchange. An explanation that requires watching its
mechanism or evidence can instead take full-screen visual coverage with
the same uninterrupted voice-over. Use `avatar_media` only where both
simultaneous narrator presence and supporting information help the viewer.

Choose between these possibilities from the central visual purpose of the
narration and the viewer's present need. The four carriers are editorial
instruments rather than an automatic sequence or a ratio to satisfy.

## Ownership, visual causality and coherent sequences

Establish who speaks, who acts, who experiences consequences and which
subjects or objects carry the relevant state. The narrator's voice does not
assign their identity to the protagonist of every narrated scene. Carry
established identity, wardrobe, space, objects and physical state through
related shots. Each successive view should advance action, attention,
knowledge or an intended emotional response, while preserving causal order.

An unfolding action may need several successive `media_video` beats showing
separate, meaningful continuous views. A useful static observation within
that progression can be a `media_image` insert. Direct **one continuing
scene** across video starting frames, still inserts and any temporary
presenter interlude. In the intended image-conditioning plan, the
reference frames belong to a single ordered sequence even when the
voice-over edit briefly cuts to the avatar. Preserve scene identity,
prop state and reveal order in every frame. Those reference frames belong to the same numbered grid whenever they depict one continuing scene. Do not change the story, the moving carrier or its temporal logic merely to fit a narrower still-only interpretation of the grid.

For abstract narration, visually communicate the stated mechanism, cause,
comparison or consequence rather than supply a merely topical object.
When the essential contribution is the narrator's own interpretation or
human connection, give the speaking presenter the screen. The result is
an intelligible alternation of people addressing the viewer, unfolding
filmed events and deliberate static details, with no preset distribution.

## Readable symbols and postproduction direction

The visual narrative may depend on source-established names, numerals,
labels, brand identities, matching details or precise comparisons.
Image-generation prompts describe the photographable frame: physical
subjects, objects, positions, viewpoint, lighting, interaction and
plausible visible states. For screens and documents, keep semantic
information about their displayed contents exclusively inside the final
`(For context: ...)` note, as required by `rules/screens_documents.md`.

Preserve necessary readable information through the appropriate source
asset and image-generation preparation; physical scene content is a visual
direction responsibility. Treat designed on-screen typography or graphics
as separate postproduction operations **only when the edit deliberately
adds them**. For such operations, supply a concise source-anchored
`editor_notes[].instruction` string that tells the editor exactly what
element to add and where or when to add it. Do not generate notes whose
job is to check the presence of logos, judge image correctness, demand
source verification or restate the desired composition. If the original
asset required for precise content is unavailable, leave it as an
unresolved asset dependency rather than disguising it as an editor note.
Preserve the screen/document prompt restriction and code-owned CTA overlays.

## Whole-video visual standard

Before submitting, review all beats and connected sequences for narrative
ownership, faithful information, physical continuity, reveal order,
**moving coverage for unfolding action**, purposeful static inserts,
**natural recurring presenter contact**, intelligible rhythm and precise
editor notes. The viewer should encounter an evolving
sequence of concrete and purposeful pictures throughout representable
narration. Maintain the existing source, speaking-avatar, grids, image
subshots, CTA, image/video generation and postproduction contracts.


## Grid continuity across carriers

When a scene is visually continuous, treat all of its generated reference assets as one numbered grid even if some beats are `media_video`, others are `media_image`, and others are `avatar_media`. An intervening `avatar` beat does not break that grid. Preserve identity, props, state and reveal order across the full sequence.


When a continuing scene forms a grid, track the generated assets in ordered
labels such as `A1`, `A2`, `A3`. Those labels persist even if the scene spans
multiple groups or is temporarily interrupted by avatar-only beats. Do not call
something a grid unless the continuity contains at least two generated assets.
