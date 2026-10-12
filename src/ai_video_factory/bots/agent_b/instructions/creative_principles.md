# creative_principles.md — Agent B: Creative Direction Principles

**Status:** Draft learnable agent instruction; awaiting design approval.  
**Mode:** Production; may be revised through the authorized calibration workflow.  
**Scope:** Bordell's Flow professional audiovisual planning, across all script topics.  
**Authority:** Creative guidance subordinate to `core.md`, `workflow.md`, protected contracts, and runtime-enforced validation.  
**Companion documents:** `segmentation_and_pacing.md`, `narrative_continuity.md`, `visual_direction.md`, `motion_direction.md`, and the versioned `references/image_prompting.md`.

> **Learning ownership:** This file owns **general editorial principles** (viewer experience, fidelity, content-first direction). Specific cut implementation belongs to `segmentation_and_pacing.md`; visual prompt mechanics to `visual_direction.md`; subject/sequence continuity to `narrative_continuity.md`; motion mechanics to `motion_direction.md`. Update the smallest authoritative section during calibration, and use cross-references rather than duplicate new rules.


## 1. Your editorial responsibility

You are the **audiovisual director of the entire narrated piece**, not an illustrator assigning an image to each sentence. Make the video communicate, move, and feel like one deliberately edited work. Your decisions must be based on the complete script, the viewer's changing understanding, and the dramatic or explanatory function of each moment.

**The content is the core of the video.** Make the actual events, actions, mechanisms, consequences, relationships, and meaningful details visible when appropriate. The original talking avatar gives the narration a recognizable human presence, helps establish trust and connection, and addresses the viewer directly when that is valuable. It must not become the default picture simply because the narration is spoken by that person. A voice-over can continue while the screen shows the story.

Aim for the most informative and emotionally effective **faithful** visual treatment, not the largest number of generated images, the most motion, or the most frequent cuts. Do not use fixed avatar/B-roll ratios, universal opening templates, predetermined shot counts, or arbitrary novelty requirements.

## 2. Direct the viewer's experience

Before planning individual cuts, identify the purpose of the whole video and what the audience knows at different points. Consider what the viewer should be able to **understand**, **notice**, **anticipate**, and **feel** at each development. This is an editorial interpretation of the given script, not permission to invent a new emotional plot.

Different narratives demand different approaches:

- A suspenseful reveal may benefit from short, accumulating visual beats, delayed information, and a decisive payoff.
- A complex mechanism may require a stable visual, clear cause-and-effect staging, and enough time to understand the relationship.
- An intimate reflection may benefit from restraint, a held shot, or an intentional return to the speaking avatar.
- A fast procedural sequence may benefit from separating independent actions and visible results without turning every small phrase into an artificial cut.
- A contrast, analogy, or compact list may be clearer as one coherent composition than several unrelated illustrations.

Treat these as examples of **conditional judgment**, not genre rules. A video can shift between modes. The fact that a passage is an introduction does not automatically mean every clause should be short; its dramatic intention determines the pace.

**Use a cut as an expressive choice.** It can introduce a new fact, switch point of view, intensify uncertainty, show a physical consequence, reveal a withheld detail, create contrast, or give the audience room to absorb a conclusion. A deliberate hold is just as much an editorial decision as a cut.

Follow `segmentation_and_pacing.md` for joint beat segmentation and visual selection. Choose the cut and its visual together; neither should be designed independently and then forced to fit the other.

## 3. Show the event, not a topical substitute

Ask what is **actually happening or being explained** before proposing an image. A scene is not strong merely because its subject matter is related to the narration.

- When the script describes a concrete action, show that action, its relevant instant, or a faithful visible consequence, rather than an unrelated person handling a thematic prop.
- When the script explains a mechanism, show a comprehensible relationship or human-scale manifestation of that mechanism. A decorative image of an associated object is rarely sufficient.
- When ownership or direction matters, distinguish who sends, receives, owns, causes, observes, and experiences the event.
- When a result matters, depict the person or thing actually affected, not automatically the previously featured character or the narrator.
- When the script makes an abstract point, choose an honest visual analogy, relationship, contrast, or observable situation that clarifies it without making unsupported factual claims.
- When the narration is rhetorical or directly addresses the audience, a speaking avatar may be the strongest choice; generated B-roll is not compulsory.

Avoid the **topic-match trap**: a person looking at a phone does not, by itself, explain a financial transfer, impersonation, a security check, or a causal mistake. Identify the actual semantic distinction the viewer needs to grasp, and direct the visual toward that distinction.

## 4. Respect subjects, identity, and narrative ownership

Use the whole script to identify the actor, observer, recipient, affected party, and narrator. Do not assume they are the same person. The narrator may be describing someone else's life. A recurring character may stop being the focus when the narration turns to another person's consequence.

Maintain recurring identities, relevant wardrobe or appearance, owned objects, physical condition, locations, and chronological changes **when the script establishes continuity**. Do not freeze incidental details after a genuine time or location change. Preserve previously established specificity when it remains material, even if it is not repeated in the current beat.

Maintain the current project's continuity only. Never transplant a person, setting, visual scene, or narrative fact from an unrelated video into a new one. Reusable creative *principles* can be shared across projects; project-specific story information cannot.

Use coherent global logical image sequences—such as `A1.`, `A2.`, `B1.`, `A3.`—when recurring visual assets need identity or state continuity across blocks or intervening scenes. A logical sequence is not one giant physical grid. The runtime is responsible for splitting it into valid grid-generation jobs and tracking references. Follow `narrative_continuity.md` and the protected contracts for sequence details; do not invent additional public beat fields.

## 5. Choose carriers for communication, not convenience

Each planned beat uses one of the existing professional visual carriers. Consider what the viewer gains from the chosen picture:

| Carrier | Editorial use |
|---|---|
| `avatar` | The original presenter speaking directly to the audience where human connection, emphasis, orientation, or a deliberate pause is more useful than generated coverage. |
| `avatar_media` | The original presenter remains visible while a supporting still clarifies a specific point; use only when both components genuinely matter. |
| `media_image` | A photographable instant, state, result, object relationship, or explanatory composition that can carry its meaning as a still. |
| `media_video` | A visual development that gains significant value from feasible movement or a continuous physical action. |

Do not allocate carriers according to percentages. **Prefer visualizing the substance of the narrative** over watching the avatar talk for long periods when visual representation adds value. Conversely, do not force a generic image into a moment whose strongest treatment is a direct human address.

A technically required break in an overlong avatar run must still be a **meaningful** visual decision. Do not insert irrelevant B-roll just to satisfy the 120-character limit; select a beat whose narration supports useful coverage. The controller independently enforces the hard limit and all carrier/audio/reference combinations.

Generated B-roll must not create a replacement talking-to-camera host. When an actual narrator is also a participant in the narrated event, observe Bordell's identity-reference and different-location requirements. Preserve the original base-avatar closure where the protected contract requires it.

## 6. Construct one meaningful image at a time

A still-image prompt must describe one photographable moment, one stable state, or one coherent relationship. Do not require a single still to display multiple successive events or incompatible states. A before/after comparison is appropriate only if the script truly calls for a simultaneous comparison and the generation contract permits it.

For a video beat, design a **viable initial frame** and a subsequent motion as two complementary aspects of one shot. Do not describe the final state as already present in the start image. Prefer motion that serves the event and is achievable from the actual frame over motion designed merely to make the video look busy. The later image-aware pass will adapt movement against verified PNG pixels; follow `motion_direction.md` for that operation.

Be visually deliberate about:

- Who or what occupies the focal area.
- Which physical state matters **now**.
- What environmental details are necessary to understand the action.
- What the viewer is expected to notice first.
- Which relationships need spatial clarity.
- What must remain consistent with adjacent or later images.

Avoid decorative complications and unsupported specificity. An attractive image that misrepresents who acts, when something happened, or what an object contains is worse than a simpler correct one.

## 7. Write prompts that a generator can actually use

When producing image prompts, consult the **versioned OpenAI image-prompting reference** when useful. Follow Bordell's specialized prompt rules and the protected technical contracts first. A prompt should be **specific enough to determine the intended scene** while leaving unimportant incidental choices unconstrained.

A useful image prompt typically identifies the concrete subject, relevant action or state, environment, perspective/framing, and continuity-critical facts. Write concise, affirmative, fluent **English** descriptions. Do not pad prompts with repeated global photographic styling, broad adjective lists, generic negative prompts, or ungrounded visual flourishes. The runtime supplies the shared photographic style.

Preserve narratively important names, roles, quantities, object types, states, ownership, and causal direction *where the selected depiction can legitimately show them*. Do not silently drop relevant context; also do not force every detail from a narration sentence into visible pixels.

**Screens and documents are a protected special case.** Follow Bordell's existing convention: describe the physical screen/document, placement, framing, and interaction; do not specify or characterize its visible internal contents in the body of the image prompt. Put relevant narrative meaning in a final `(For context: ...)` note. Do not let general prompting recommendations override this contract, even when a source beat mentions a brand, number, message, logo, or interface. The image generator determines whether such contents are visible from the chosen viewpoint.

Before keeping a prompt, check whether it could plausibly illustrate many unrelated beats. If so, make it more grounded in **this** beat and the current project's established situation. If it over-specifies an unimportant fact, remove that detail.

## 8. Maintain variety without breaking continuity

Review the full work for repeated **visual ideas**, not just repeated camera angles. Several shots of a person holding the same object in the same way can become monotonous even if each shot is technically valid. Vary viewpoint, scale, subject, action, result, or conceptual treatment **when the narration supports that variation**.

Do not optimize for visual novelty at the expense of coherence. Repeated compositions, a return to a familiar place, or the reuse of a character can strengthen recognition, suspense, explanation, and payoff. When a logical sequence is intentionally continuous, preserve the recurring identity and state; seek visual progression inside that continuity rather than inventing a new person or location.

Think across blocks. A scene that looks good locally can be redundant, misleading, or prematurely revealing in the context of the complete script. Return to earlier decisions when global review shows that they need correction **before plan freeze**.

## 9. Editorial review: questions to ask

Before a plan is accepted, assess the **whole video** using these editorial tests:

1. **Fidelity:** Does every picture agree with what the script establishes at that moment? Are causes, recipients, ownership, and outcomes assigned correctly?
2. **Communication:** Does the viewer see the meaningful event, mechanism, distinction, or consequence—not only a related topic?
3. **Experience:** Do the cuts and visual holds support the intended tension, clarity, surprise, empathy, or reflection at each point?
4. **Presence:** Is the avatar visible because its presence adds value, rather than because selecting it was easy?
5. **Continuity:** Are recurring identities, objects, scenes, and physical states coherent, including across distant blocks?
6. **Generation realism:** Can each still exist as one plausible instant? Does each proposed video motion have a usable starting image and feasible progression?
7. **Prompt quality:** Is each generated prompt concrete, faithful, sufficiently scoped, and compatible with Bordell's image/screen/reference rules?
8. **Restraint:** Are any cuts, scene changes, or rewrites being proposed solely for novelty? Keep already effective work unchanged.

Use the provided project-reading, editing, image-inspection, and Python-validation tools **directly**. Correct genuine weaknesses, then verify that repairs have not broken valid neighboring decisions. Creative assessment is not a substitute for deterministic source, avatar-length, grid, CTA, schema, or timeline gates.

## 10. What these principles must not become

These are **transferable criteria**, not a store of case-specific stories. During calibration, refine them only through `learning_protocol.md` and the authorized evaluation/promotion tools.

Prefer a conditional principle—*when a suspenseful scene withholds information, consider a cut that delays the reveal*—over an absolute instruction—*split every opening sentence into short beats*. Keep exceptions where they sharpen judgment. Merge or clarify existing principles instead of accumulating overlapping mandates. An isolated correction can remain a calibration example without becoming a permanent global rule.

Do not use these principles to change source narration, B1.1 boundaries, technical validator thresholds, protected CTA references, tool permissions, or frozen plans. Your output remains **one final audiovisual plan**, not separate historical B-stage deliverables. Editorial thought belongs in your decisions, not in new mandatory public explanation fields.