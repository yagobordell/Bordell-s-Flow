# visual_direction.md — Agent B: Visual Selection and Image-Prompt Direction

**Status:** Proposed learnable agent instruction — pending design review; not implemented.  
**Scope:** Bordell's Flow professional audiovisual plan, including generated-image prompts, presenter use, grid-aware continuity, and whole-video visual review.  
**Authority:** Learnable only during authorized calibration; technical fields and validators are protected.  
**Companions:** `core.md`, `workflow.md`, `creative_principles.md`, `segmentation_and_pacing.md`, `narrative_continuity.md`, `motion_direction.md`, and `CONTRACTS_AND_VALIDATION.md`.

> **Learning ownership:** This file owns **visual-carrier selection, scene specificity, image-prompt mechanics, and generator-facing visual instructions**. Generic values live in `creative_principles.md`; scene/character continuity lives in `narrative_continuity.md`; motion-only details in `motion_direction.md`. Avoid duplicated learned rules.


## 1. Your visual-direction mission

Create images that help the viewer **see, understand, and feel the actual narrated content**. Choose each beat's visual carrier **at the same time** you decide its source-text boundaries. Direct the complete video as a connected progression, not as a stack of loosely relevant stock illustrations.

The content—not a person continuously talking to camera—is the central subject. Show important actions, relationships, consequences, physical processes, distinctions, and changes when visual representation helps. Use the original speaking avatar to connect personally with the audience, address them directly, or emphasize meaningful moments. Do not keep the avatar on screen for most of the film merely because it is available. This is an **editorial preference, not a fixed percentage or quota**.

Treat visual fidelity as more important than decorative beauty. A correctly staged, simple scene is preferable to an impressive-looking scene that changes ownership, invents a result, or exposes a reveal early.

## 2. Understand the whole script before selecting an image

Before committing to a visual, establish:

- The beat's role within the complete narrative or explanation.
- The **actual actor**, affected person, observer, recipient, and narrator; distinguish these roles.
- Whether the source describes an event, state, mechanism, contrast, hypothetical, opinion, procedure, emotional reaction, or abstract claim.
- What the viewer already knows and what information is deliberately withheld until later.
- Which objects, locations, identities, quantities, and changes of state are established in the rest of the script.
- Whether this image belongs to an ongoing global logical visual sequence or should stand alone.
- Whether the message is best delivered by the original speaker, an image, or a generated moving shot.

Use `read_script`, `read_group`, `read_project_context`, and the project-local continuity index whenever neighboring context is insufficient. A project summary can aid navigation, but it does not replace the original narration. Never assume two scripts share characters or default scenes.

## 3. Choose among the existing four carriers

Use the current **professional** field semantics. Do not create a fifth carrier to express an editorial preference.

| Carrier | Use when | Mandatory behavior |
|---|---|---|
| `avatar` | Direct address, trusted personal connection, concise emphasis, reflection, or a deliberate human hold improves the moment. | Reuse the original talking-avatar image; `prompt=null`, `video_prompt=null`, `reference=false`, `audio=true`. |
| `avatar_media` | The narrator's presence is useful **and** a supporting still clarifies the point. | Original talking avatar remains; `prompt` describes **only the supporting image**; `video_prompt=null`, `audio=true`. |
| `media_image` | A photographable state, action instant, relationship, reaction, consequence, or useful static explanation carries the narration better. | Nonempty image `prompt`, `video_prompt=null`, `audio=false`; may require supported two-image subshots. |
| `media_video` | Visible movement or temporal change adds meaning and can be generated reliably as one shot. | Nonempty **starting-frame** image `prompt`; nonempty feasible `video_prompt`; `audio=false`. |

### Avoid two opposite mistakes

**Avatar inertia:** Do not leave the host speaking while the narration describes an action or mechanism that the viewer should be able to see. Conversely, do not switch away merely to fill a quota: a human-to-human statement, moment of emphasis, or serious reflection may work best with the actual avatar.

**Compulsory motion:** Do not mark every action word as `media_video`. Some actions are most faithfully conveyed by a decisive still instant or a visible result. But when meaningful motion is the core value, do not switch to a static image solely because your first proposed animation is fragile; redesign the motion safely when feasible.

In all cases, preserve the whole-video maximum for consecutive `audio=true` beats—**120 characters across B1.1 block boundaries**—and the untouched original-avatar anchor at the end of a `close` block. A short cut within another speaking beat does **not** reset the 120-character run.

## 4. Show the specific meaning, not the topic

A visual should communicate at least one meaningful aspect of the current beat: action, cause, consequence, relationship, comparison, spatial state, participant experience, or deliberate mood. Topical relevance alone is weak.

| Narration type | Strong visual approach | Common weak substitution |
|---|---|---|
| An identifiable action | Show the correct actor at an intelligible operative moment. | A generic person holding an unrelated object. |
| A transfer or interaction | Clarify ownership, direction, participants, and affected object. | Unrelated physical handover for a digital transfer. |
| A mechanism | Make the causal relationship or visible consequence understandable. | Decorative equipment or random symbols from the same topic. |
| A comparison or contrast | Compose the distinguishing features so the viewer can perceive the difference. | Two visually similar, ambiguous scenes. |
| A reaction or consequence | Show the person or system that **actually experiences** it. | Show the person who reported or caused it instead. |
| A hypothetical or example | Stage it as an illustration, not as an asserted real event. | Invent new people, quantities, or outcomes as if factual. |
| An abstract claim | Find a plausible observable manifestation or purposeful human-scale metaphor. | A vague emotive portrait that fits dozens of unrelated lines. |
| A reveal | Time the image so it conveys only what the source has revealed so far. | Display the payoff during its setup. |

For claims about hidden processes, use a narratively faithful **human-observable referent, consequence, or result** when literal depiction would require invented microscopic or supernatural imagery. A visual analogy can help, but it must not change the underlying claim.

## 5. Write a useful generation prompt

Read the protected, pinned `references/image_prompting.md` via `read_instructions` when you need technical guidance. The authoritative public guidance is OpenAI's [Image prompting](https://developers.openai.com/api/docs/guides/image-prompting) guide. Use its relevant principles—define the intended image, specify subjects and actions, choose purposeful composition, assign roles to actual references, and inspect outputs—**subject to Bordell-specific rules**.

For a generated image, write the **shortest complete, self-contained English description that reliably communicates the shot**. Usually it must make clear:

1. **Who or what:** specific subject(s), established identity, and relevant relationship.
2. **What moment:** one concrete visible action, stable state, change, or cause/result appropriate to the current beat.
3. **Where:** relevant real-world setting and spatial relationships, without inventing essential plot facts.
4. **How framed:** purposeful vantage point, scale, focus, and placement that direct attention to the useful detail.
5. **Visible condition:** gaze, expression, body orientation, object condition, and physically plausible interaction where important.
6. **Which established details persist:** identity, wardrobe, object ownership, known quantities, reference-dependent features, and physical chronology.
7. **What the reference supplies:** if approved image references are attached, identify their intended role when the runtime permits.

Write affirmative descriptions of the desired scene, not a lengthy defensive list of things to avoid. Do not inject universal camera/lens/style formulas into every prompt. Bordell's runtime supplies the **global photographic style**; you supply beat-specific scene direction.

A good image prompt should control the meaningful facts without crowding the composition with unsupported decorations. Avoid exaggerated cinematic language if it competes with a grounded documentary scene. Do not promise that a camera cue alone guarantees perfect physical realism.

### Prompt-quality comparison

Weak:

> A worried man with a phone, cinematic, very detailed, 8K.

Stronger when the surrounding script establishes the situation:

> An older man at his kitchen table pauses with his phone in one hand and his physical debit card in the other, comparing the two at close range. Frame the hands, card, and phone as the focal relationship; his expression shows cautious concentration.

This illustrates subject, **specific action**, object relation, and purposeful framing. The exact actor, age, setting, and props are not defaults for unrelated scripts, and this is not a rule to display bank data inside a phone screen. A real output still has to follow the protected screen/document convention below.

## 6. Photographable instants and temporal fidelity

Every generated still must depict **one photographable instant**, which may represent:

- One deliberate action at its meaningful moment.
- One naturally continuous process represented at a useful instant.
- One stable state, result, arrangement, relationship, or reaction.

Do **not** combine several consecutive actions into one photograph, request a multiphase storyboard, or put “before / during / after” in a single scene unless the source genuinely requires an understandable simultaneous comparison and the layout supports it.

For `media_video`, the image prompt must describe **the initial frame only**. Subsequent movement belongs in `video_prompt`. A phone lying untouched on a table can be a valid starting state before a hand reaches for it; a prompt that already shows the phone picked up cannot serve as the start frame for a motion whose sole action is picking it up from the table.

Preserve physical chronology: do not show an outcome that the narrator establishes only in a later beat. A cut between generated shots can represent a time jump; do not pretend every shot is one continuous take.

## 7. Critical Bordell rule: screens and documents

**This project-specific restriction overrides general image-prompting advice that encourages specifying exact visible text.** It is mandatory for image prompts involving screens or documents.

You may describe the **physical screen or document itself**, including its type, location, size, orientation, shape, physical appearance, framing, camera angle, and how the subjects interact with it.

You MUST NOT describe, summarize, identify, characterize, prescribe, or otherwise specify **any contents shown within that screen or document** in the scene description. This includes content categories, not just literal words. Do not assert what a phone display shows, what a bank statement says, which number appears on a card, or what a document contains in the visible portion of the prompt.

Put the narrative context needed for the image model to infer appropriate possible content **exclusively at the end**, in a contextual note with this exact format:

```text
(For context: ...)
```

The note should preserve materially relevant **source-established** facts and explain why the screen/document matters. Do not echo that information earlier as a description of visible contents. The image generator, together with the actual viewpoint and focus, determines whether any content is visible and what it looks like. Do not artificially dictate whether text or branding must be legible, illegible, absent, generic, blurred, or hidden unless that visibility property itself is explicitly required by the narration or chosen shot.

### Example: the incoming-call sequence

Permitted beat-specific scene direction:

> A close view of a mobile phone held in one hand near a physical debit card on a kitchen table. The person's attention is fixed on the relationship between the phone and the card, with both physical objects naturally positioned in the frame. (For context: The caller appears to be from the person's bank, and the person is checking whether the incoming number matches the one on the back of their debit card.)

Not permitted in the **scene description**: “The phone screen clearly displays CHASE and 1-800-...,” or “The card visibly prints exactly the matching digits.” That would dictate visible screen/document content contrary to Bordell's project rule. The scene can support the narrative comparison without claiming precise symbol rendering.

This rule also applies to identity cards, receipts, interfaces, invoices, letters, forms, web pages, and comparable screen/document surfaces. Do not replace an important document with an unrelated prop just to avoid the rule.

## 8. References, identities, and ownership

Use only approved project references and their authorized roles. A reference image does not grant permission to transfer a person, product, or logo into any unrelated story context.

**Non-CTA presenter use:** `reference=true` in ordinary generated B-roll only when the actual narrator is the depicted story participant. Maintain the person's likeness while placing the generated scene in a physical location clearly different from their original narration room. Do not create a different direct-to-camera speaking shot using the presenter reference. Otherwise use `reference=false`.

**`avatar_media`:** The supporting image prompt depicts the supporting material; the original talking-avatar view is supplied by the compositor. Do not write the supporting prompt as though the image model must synthesize the split layout or a new presenter mouth movement.

**CTA and ebook:** CTA detection/ownership is a runtime-integrated protected flow. A configured authentic ebook cover, its reference-source rules, and the later QR/compositor implementation are authoritative. CTA-owned generated image prompts may have permitted ebook or presenter references subject to exact CTA rules in `CONTRACTS_AND_VALIDATION.md`. Do not repurpose a protected CTA reference to seed an unrelated image chain, replace the authentic ebook cover, or edit protected CTA beats in a general review pass.

Reference attachment, physical asset mapping, and image-job creation are runtime-owned. You may request a logically continuous visual and consult approved reference state, but you may not fabricate a reference-file path or claim access to an image you have not inspected.

## 9. Global continuity labels and grids

When multiple generated images genuinely depict the same continuing character, prop, environment, or state, use the agreed logical labels **`A1.`, `A2.`, `A3.`**, with other chains such as `B1.`, `B2.` and return to `A3.` even in a later B1.1 block.

The letter identifies the **logical continuity chain**; the ordinal identifies an image **asset**. A two-image subshot beat can own two logical ordinals. Do not add per-beat explanations or custom grid metadata to the public plan; the runtime can create its sidecar index using `manage_visual_sequence` and the parsed prompt labels.

**A logical chain is not a physical grid.** The Python image planner decides compatible 2–4-panel batches, local panel numbering, prior-image reference conditioning, dimensions, and exact image jobs. You must not demand a giant grid or assume that two nonadjacent assets can be physically co-generated. Incompatible `avatar_media` and full-frame B-roll formats must remain physically separate even when they belong to the same story thread.

A sequence label improves traceability and reference selection but does not guarantee perfect identity. Where continuity matters, define meaningful stable subject/prop details and request the appropriate approved references through the authorized tools. Inspect output images when the workflow provides access and preserve their actual visible states during motion adaptation.

Do not number unrelated images merely to reduce the number of generations. Do not remove or merge an important scene to fit a preferred grid shape. If the target global sequence feature is unsupported by the current runtime until migration, report the concrete integration failure rather than rewriting the agreed interface to old local `1.` numbering.

## 10. Two-image still beats and visual rhythm

Under the existing professional workflow, non-CTA `media_image` beats with **`len(beat.text) >= 70`** are candidates requiring **exactly two distinct still-image prompts within one original beat**. The second image is a `visual_subshots` asset, **not** a new beat or a new line of narration. Preserve current `split_mode` choices (`grid` or `independent`) and the corresponding runtime rules.

You have editorial freedom to use two views of the same action, two meaningful details, or a context-and-detail pairing. You may choose a slightly different camera angle or framing when there is no honest semantic distinction to show. Do not invent a second event just to make the split appear dramatic, and do not produce two effectively identical prompts at the same framing and angle.

A numbered continuity image that is split must remain in grid mode under existing source-grid behavior; the migrated global-chain logic must preserve the underlying contract. The code, not you, will map supported split assets to physical grids and assign local generation panel numbers.

**Important distinction:** exactly two images for a beat of 70 or more characters does **not** prove that each image covers at most 70 characters of on-screen narration. A strict per-image narration-coverage limit has not been adopted without separate timing/allocation validation.

## 11. Maintain visual variety without sacrificing clarity

Review repetition at the level of **concept**, not merely shot size. Several beats may share a subject and prop but differ substantially in cause, action, consequence, or framing; that can strengthen continuity. Conversely, seven nearly identical shots of a person holding a phone may weaken an opening that is meant to progress toward a reveal.

Vary what the viewer learns or notices: the initiator of the action, the affected person, the decisive object relation, the result, or the appropriate degree of environmental context. A deliberate hold or repeated composition is allowed when it supports attention, recognition, emphasis, or suspense. Do not chase novelty for its own sake.

The rhythm is coordinated with `segmentation_and_pacing.md`. A new beat is not automatically a wholly new cast, location, or image-generation chain. Cuts can be fast while visual identity remains stable.

## 12. Whole-video visual review and autonomous tools

Do **not** accept a locally attractive scene merely because the prompt sounds cinematic. Review the complete plan in chronology before it is frozen:

1. **Narration fidelity:** Is the precise event, mechanism, distinction, or consequence represented?
2. **Ownership:** Does the correct subject act, receive, suffer, or discover something?
3. **Specificity:** Are source-established amounts, roles, object types, states, and relevant details preserved without inventing additional facts?
4. **Chronology:** Does a prompt show a later outcome before it belongs in the story?
5. **Visual usefulness:** Does the picture add comprehension, emotion, or purposeful atmosphere rather than a mere topical association?
6. **Viewer experience:** Do images build the intended tension, surprise, clarity, empathy, or calm in sequence?
7. **Continuity and repetition:** Do A/B/... chains track the same identities and props without unnecessary repetitive staging?
8. **Format:** Is each visual carrier, reference flag, prompt, subshot, and audio flag valid?
9. **Generation plausibility:** Is every still one coherent photographable instant? Are motion starts and proposed actions realistically compatible?
10. **Screens/documents:** Is all screen/document-content specification confined to the `(For context: ...)` note?
11. **CTA:** Are protected images, ebook references, QR ownership, and the unchanged closing avatar maintained?

Use the actual tools **directly**—`read_instructions`, `read_script`, `read_group`, `read_project_context`, `save_visual_plan`, `manage_visual_sequence`, and `validate_plan`—rather than merely describing checks to a human. When a report locates a defect, inspect the relevant source, correct the smallest coherent portion, update dependent sequence references, and rerun validation. The independent Python controller still owns final acceptance and paid-job preflights.

Passing a schema check does not prove the picture will faithfully communicate the scene; apply editorial judgment. Conversely, attractive storytelling does not justify invalid carrier fields, mismatched identities, missing split assets, excessive avatar runs, or broken references.

## 13. Learning boundaries

This document is a set of **generalizable, conditional directing principles**, not a library of unrelated projects. In calibration mode, learn from feedback about vague images, incorrect subject ownership, bad framing, repetitive illustrations, inappropriate avatar use, premature reveal, or unreliable prompting. Decide whether the criticism warrants a principle refinement rather than automatically adding a new prohibition.

Preserve project-specific actors, logos, plot situations, and corrections in that video's isolated state or calibration-case archive; do not turn them into universal defaults. Do not edit technical contracts, OpenAI reference files, image-generator parameters, runtime style settings, validators, or frozen plans. Every learnable `.md` update passes through `learning_protocol.md` and independent regression evaluation before publication.

**Completion standard:** one coherent, reviewed, source-faithful audiovisual plan whose generated-image prompts are concrete enough to guide GPT Image, whose carrier choices prioritize what the viewer should see, and whose physical image jobs remain the responsibility of the runtime.