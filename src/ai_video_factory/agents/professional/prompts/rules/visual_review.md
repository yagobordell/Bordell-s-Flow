# Mandatory whole-video visual review — B2.1 editorial fidelity

These editorial safety/fidelity standards carry forward from B2.1. The NEW director can resegment beats during correction; the old B2.1 prohibition on changing beats was an interface restriction, not a creative principle.

Mandatory whole-video visual audit
Evaluate every editable generated visual in the context of the COMPLETE video, not in isolation.
Continuity and narrative ownership
Keep recurring people, objects, locations, wardrobe, physical state, time/state and causal state coherent when the narration implies they are the same.
Resolve who owns each action, experience, possession, consequence or reaction from the complete narrative context.
Show the person or object actually affected by a consequence.
Do not keep following a previously introduced person merely because they appeared in the preceding shot when the narration has shifted the action or consequence elsewhere.
Do not transfer a consequence to the person who caused, reported or observed the event when the narration assigns that consequence to somebody else.
Narrative and causal fidelity
The visible event, state, relationship, reaction or process must agree with what the narration says is happening at that moment.
When narration states a concrete visible action or temporal development, the
visual should depict its actual progression in a motion-carrying shot with
its correct participant and consequence. A static frame can communicate a
stable state or decisive detail but cannot itself portray the unfolding
movement. Judge the intended visual contribution, not mere topic matching.
Show the actual nature and direction of a narrated action or transfer through a corresponding visual relationship.
When narration explains a mechanism, relationship, transfer, comparison, sequence or causal distinction, the visual must communicate that mechanism rather than merely show a related person or prop.
Narration-grounded specificity
Compare every editable prompt against the beat's frozen narration and relevant facts established by the whole-video context.
If a concrete detail is already established, materially relevant to the visual, and the current prompt omits, weakens or generalizes it, restore that detail.
Preserve relevant source-supported specificity including amounts, quantities, named entities, object types, participant roles, ownership, sender/recipient directionality, locations, physical states, established appearance and continuity-critical distinctions.
A detail established elsewhere in the video may remain relevant even when it is not repeated in the current beat.
Do not mechanically copy narration into prompts.
Do not add incidental detail that does not improve fidelity, clarity or disambiguation.
Do not invent story facts or outcomes; ordinary plausible staging details are welcome when they make a scene specific and do not contradict established information.
Visual usefulness
Prefer a visual that adds comprehension, emotion, evidence, causality, spatial understanding or deliberate atmosphere over a merely related generic illustration.
Make the relevant subject or prop communicate its precise narrative function.
When the beat explains a mechanism, consequence, distinction or process, prefer a composition that makes that information understandable.
Variety across the whole piece
Notice repeated visual concepts, not just repeated shot sizes.
Reduce repetition when repeated subject + dominant prop + staging weakens the piece.
Do NOT chase novelty for its own sake.
A visually similar shot is allowed when continuity, clarity, rhythm or emphasis benefits from it.
Changing only camera angle or framing is valid when the underlying visual concept is already strong. It is not a substitute for correcting a weak or generic concept.
Generated-image semantics
Every generated image prompt represents one photographable instant. For
`media_image`, that instant has the viewing purpose of a stable detail,
state, outcome, evidence, comparison or arrangement. For `media_video`,
the image prompt describes a motion-ready starting frame and the
`video_prompt` describes the continuous physical development that follows.
Keep successive temporal stages in the moving footage rather than
compressing them into one still image. Compose simultaneous static
comparisons only when the narration calls for inspecting that relationship.
Do not place later temporal stages into the video starting frame.
Generated visuals must remain physically plausible and human-observable at ordinary scale. For hidden mechanisms, prefer a visible human-scale referent, consequence or result rather than invented microscopic imagery.
Physical-state chronology
Preserve physical state in narrative order.
Do not show a later result before the narration establishes the transition that creates it.
If an object is established in one state, preserve that state until narration introduces or clearly implies the change.
If the literal transition cannot be generated reliably, re-stage the motion while preserving the current state.
A later beat may show the completed state once narration has established it.

## Review carrier choice against the narrated experience

For every beat, assess the task the viewer is expected to perform: follow
something happening, inspect something stable, or connect with the speaker.
Compare the chosen shot to that need and to surrounding shots. A technically
valid visual carrier is not evidence that the film tells the story well.

- When the story contains visible action, changing behaviour, reaction or a
  time-dependent consequence, verify that `media_video` makes the actual
  movement intelligible. Detect still poses and multi-still grids that have
  replaced movement with disconnected snapshots; ask for a feasible moving
  representation when that is the stronger faithful depiction.
- When the narration focuses on readable or stable information, verify
  that `media_image` grants that information prominence and sufficient
  viewing time. Detect moving footage that distracts from a detail that
  would be clearer as a still.
- When the presenter's voice speaks *to* the audience, verify that
  `avatar` returns as a recognisable human presence where direct address,
  explanation, guidance, warning or interpretation benefits from eye contact.
  Detect long visual runs that have displaced that communicative role with
  unrelated static coverage. Equally, assess whether prolonged speaker
  footage suppresses a meaningful narrated event.
- When `avatar_media` is used, verify that the side-by-side composition
  improves the message instead of splitting the viewer's attention.

## Review reference-chain continuity and editing orders

Follow the same narrative scene through moving-shot starting frames,
static detail images and any intervening avatar return. Shared identity,
wardrobe, geography, object states and the order of discoveries should
survive all these visual cuts. The numbered grid may contain generated assets from different carriers when they belong to the same continuing scene. Review that wider continuity chain as one coherent conditioning sequence rather than fragmenting it into still-only islands.
Evaluate editor notes as intentional, additive postproduction commands,
not descriptions of the shot, reminders about brand identity or quality
checks. Every `instruction` should directly order an observable edit.


Inspect the actual sequence and the function of each picture, including
repeat appearances of the same subject and transitions between voice-led
address and filmed action. Review every group in the context of the full
video's changing narrative intensity and maintain source fidelity,
continuity, reveal timing and technical feasibility. Return an actionable
finding for every specific mismatch; no carrier-percentage target or
carrier-name shuffle is a substitute for editorial justification.


## Review grid logic as scene logic

Review grids as scene-continuity devices, not as still-only containers. If the same scene continues across different carriers, its generated assets should normally remain in one numbered grid. Flag plans that isolate a `media_video` starting frame or an `avatar_media` support still from the rest of its own scene without a real scene change.


Check the grid labels themselves: the letter must identify the logical grid in
its global appearance order, and the numeric suffix must reflect the asset's
position within that grid. A valid grid always has at least two labeled assets.
