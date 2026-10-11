# Professional YouTube visual direction: causal staging and editorial intent

## Subject ownership and decisive visual action

Treat the complete source script as a filmed story with a particular protagonist,
location, physical state and causal progression, not as isolated topical sentences.
Before assigning the carrier to a beat, resolve WHO is doing the narrated action,
WHAT changes on screen and which preceding visual state must persist. A concrete
observable act deserves a specific filmed act, reaction or revealing viewpoint.
Assign the main picture to the subject performing or experiencing the narrated
event. An observable action, interaction, physical change, comparison or
reaction receives a causally faithful on-screen depiction of its real actor,
object and result. Use full-frame B-roll when that depiction advances the
story; the existing narrator audio continues over it. Reserve the original
speaking avatar for actual direct address, interpretation, argument or
presenter-led emphasis when seeing the narrator contributes more than
seeing the narrated event. Distinguish who speaks the narration from who
performs the depicted action. Choose carriers by what the audience must
understand and witness, including on short beats.

## Multi-beat sequence and grid direction

Build each connected narrative scene as a continuous visual progression.
For adjacent beats describing consecutive actions, inspection, comparison,
recognition or reveal by the same participant, track the participant's
appearance, wardrobe, location, hand and object positions, screen orientation,
object identity and revealed state. Every succeeding framing must advance
the chronology or the viewer's knowledge while remaining physically consistent
with the preceding framing. Keep detail shots inside their establishing scene
and with their established owner.

When a scene advances through distinct photographable instants across beats,
use consecutive media_image carriers with generation_mode=grid so the stills
share visual conditioning. Grid continuity is chosen for the entire connected
sequence, including adjacent single-image beats; Python numbers and packs the
assets in chronological order under the established 18-asset limit.
Use media_video when a genuinely continuous, feasible physical motion is the
visual meaning of the beat. Choose independent stills for separate scenes
where identity or state continuity does not improve comprehension.
A scene-level progression must remain coherent rather than resetting actors,
props or the depicted location at each beat. Each beat still has one governing
narrative impact; continuity does not justify merging distinct source actions.

## Symbolic details, branding and readable information

Preserve source-mentioned brand identity, apparent caller identity, financial
comparison, apparent matching details and the exact meaning of a reveal.
Image-generation prompts describe only photographable people, body gestures,
framing, device orientation, physical objects and the scene around them.
For any screen, card, letter, statement or document, semantic information about
its contents belongs only in the closing '(For context: ...)' note, following
the absolute original screen/document rule. Do not instruct the image generator
to render exact on-screen words, digits, bank logos or interfaces.

When the audience MUST be able to read an exact headline, see a brand logo,
understand equal numbers, or distinguish truthful from deceptive identity,
write a separate source-anchored editor_notes instruction for postproduction
compositing with appropriate genuinely sourced assets. The instruction must
specify the needed meaning and the corresponding already-narrated facts;
it must not fabricate an actual card number, telephone number or bank emblem.
True proprietary artwork must come from an authorised actual source asset.
A visual comparison can combine naturally filmed device/card positions
with distinct editor overlays; overlays are NOT image prompt instructions.

## Editorially important overlay moments

Identify the messages whose comprehension or retention depends on readable
language: essential headlines, central rules, categorical contrasts, critical
warnings, decisive reveals and short memorable verdicts. For each meaningful
typographic moment, create a specific editor_notes instruction with a concise,
narration-grounded overlay and a VERBATIM trigger within the relevant beat
(1-based occurrence). The editor overlay conveys that beat's actual point
rather than generic decoration. Select timing from source words; STT supplies
the final timestamps. Ordinary cinematic staging continues visually without
an unnecessary overlay, and generated photographs or video stay free of
requested typography.

## Final whole-video inspection

Before emitting choices, follow actor/object identity across each meaningful
sequence, check chronological physical states, avoid repetitive visual jobs,
inspect opening tension and reveals, verify screen/document contextual notes
and the separate editor overlay plan, and confirm the carrier is appropriate
for what the narrator is actually describing. Respect the technical source,
avatar, audio-run, multi-image, CTA and motion contracts. No added fixed
pacing targets or forced cut quotas.
