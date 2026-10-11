# B1.2 complete semantic segmentation standard — inherited from the functioning bots

This is the ORIGINAL B1.2 editorial algorithm and calibration, not a new
rhythm quota. It applies to the ENTIRE script in ONE global segmentation
request, respecting the exact B1.1 materialized group boundaries. Any
old reference to "current block", "worker", JSON-fenced output, or B1.2
error transport is adapted only in the INPUT/OUTPUT interface: we send every
group's offset range in one request, and Python reconstructs the exact text.
The original semantic tests, hard limits and audience cadence
take full precedence over generic guidance elsewhere.

## Proven beat definition, action atomicity, cadence, five boundary tests — original B1.2 verbatim

What a beat is
A beat is the smallest contiguous source unit that produces one governing, semantically complete and independently representable narrative/visual impact at a natural viewing cadence. A valid beat must satisfy atomicity, standalone completeness and rhythmic cohesion. Atomicity is defined by the source event/message, not by sentence length, topic, scene, camera continuity, punctuation or word count.
A normal beat carries one governing atomic impact: one operative action/instruction, one natural process, one state, one observable change, one reaction, one result, one claim/message, one question, one comparison/analogy, or one explanatory step. It may contain several short parallel clauses, repeated negatives, list items or sentence fragments when they jointly express that one governing rhetorical impact rather than successive staged events. It must also contain enough of the source wording to make that unit intelligible without borrowing a missing predicate, object, purpose or completion from an adjacent beat.
Action atomicity
An action beat normally contains exactly one independent operative action/instruction as its visual/narrative event. If the source gives a sequence of independently complete actions that are actually performed or instructed as successive stages, each action receives its own beat even when they share actor, object, purpose, sentence, scene, location, camera setup or beat_type. Do not treat rhetorical mentions of actions, repeated negatives or a negative/negative/affirmative parallel construction as successive operative actions merely because each clause contains a verb. A posture, immediate attention cue or subordinate continuation remains with the primary instruction when splitting it would create a fragment that is not a complete impact by itself. The only source-form exception is a compressed telegraphic cluster whose individual implied steps cannot be emitted as standalone beats without inventing omitted words; keep only the minimum such cluster together.
Procedural shorthand can encode actions without repeating verbs, but do not manufacture standalone beats from isolated shorthand tokens. Split shorthand where the source itself supplies independently intelligible operative phrases when those operative phrases are present in the source. When the remaining source is a compressed tail such as cream, socks, bed., treat it as one telegraphic checklist/summary impact rather than as three separately executed actions; do not emit cream,, socks, and bed. as orphan beats. Likewise, a nominal inventory such as the file, the cream, the sun is one list/message unless the source explicitly assigns separate actions to its items.
Several words or verbs may remain together when they form one complete operative moment rather than several independently complete steps. Manner, posture, perception, a short attention cue, or a non-deliberate physical consequence may stay with the primary event when it does not create another standalone impact. When a source action contains posture and its immediate attention cue as one operative event, keep them together; split only an independently complete subsequent action. Mutually exclusive alternatives joined by or may remain one beat when they are alternative ways to perform the same single instruction, not steps to perform in sequence.
Non-action atomicity
A non-action beat contains one governing independently representable message, state, reaction, change, result, question, comparison or explanatory step. Descriptive modifiers and short parallel propositions may stay with that unit when they collectively form one rhetorical assertion, contrast, enumeration or progression. Two genuinely autonomous moments must not be merged merely because they support the same topic.
A contrast may remain one beat when both sides are required to express the single contrastive message. An analogy may remain one beat when its elements jointly form one comparison. A reaction plus adjectives/metaphor describing that same reaction remains one beat. An enumerative list of symptoms, attributes, examples, components, ingredients, parallel roles, repeated negatives or short anaphoric/parallel clauses remains one beat when the items jointly express one set/message or one rhetorical progression rather than events that unfold separately. This rule may span commas, semicolons or several short sentences. Grouped conditions and compact role mappings remain one governing message when they are not independent staged actions.
Rhythmic cohesion and audience cadence
For intro blocks, favor a slightly quicker opening cadence: split genuinely autonomous hooks, questions, reveals, contrasts or developments when the source supports distinct visual impacts. Outside the intro, prioritize a calmer comprehension-first cadence. Neither preference permits artificial microbeats, splitting cohesive lists, or rewriting the source.
A sentence boundary is not a visual-reset command. When several short clauses share subject, rhetorical function, time, setting and governing message, prefer the smallest contiguous cluster that can be comfortably understood and represented as one beat, provided it stays within 40 words and does not hide genuinely successive staged actions or results.
Boundary tests
Apply all five tests before accepting a candidate beat or boundary:
1. Atomic-event test — Does the candidate contain more than one independently staged action, procedural step, message, state, reaction, change, result, question, comparison or explanatory step? If yes, split. Do not count short parallel clauses, repeated negatives or enumerated items as separate atomic units when they jointly serve one governing rhetorical message and do not require separate staging. A compressed telegraphic checklist whose individual tokens are not standalone beats counts as one summary message, not as several independently complete actions.
2. Single-asset test — Could one still image, one simple continuous clip, or one coherent representative composition faithfully represent the candidate's governing impact without sequencing multiple independently staged actions or moments? If no, split. A compact enumeration, parallel rhetorical cluster or compressed telegraphic checklist may pass when one summary/representative visual can carry the governing message; do not split merely because each member could also be illustrated separately. Passing this test does not by itself permit merging true independent stages.
3. Autonomous-impact test — Could a contiguous source part stand as its own event/message/moment and naturally require its own visual reset rather than merely participating in the same rhetorical set, restating, qualifying or detailing the same governing impact? If yes, it is a split candidate even if the same scene or camera could show both parts. Grammatical completeness or the mere possibility of inventing a separate asset is not enough. A closer detail or one member of a compact rhetorical enumeration is not automatically a new beat.
4. Standalone-completeness test — After the proposed split, can both sides function as intelligible visual/narrative impacts using only their own source words? Reject a boundary that leaves a bare verb/posture (sit,), connective fragment, dangling conditional/relative/subordinate clause, unresolved pronominal fragment, isolated list item, or telegraphic token that requires the adjacent beat to complete its meaning. Standalone completeness is necessary but not sufficient: a grammatically complete short clause can still be over-segmented when it is one member of a compact parallel/enumerative rhetorical run.
5. Rhythmic-cohesion test — Would accepting this boundary create a rapid run of short consecutive beats inside the same enumeration, anaphora, repeated-negative pattern or parallel rhetorical construction without a genuine staged event/result reset? If yes, reject that boundary and keep the minimum cohesive source span together. Roughly 3–6 words per candidate is a warning signal, not a universal minimum: a short beat may remain when it carries a distinct punchline, correction, question, verdict, instruction or visible event.
Independent complete actions are semantic boundaries regardless of grammar when they are genuinely successive staged actions. A sentence may contain several beats, and several short sentences may form one beat. Sentence boundaries, repeated subjects and punctuation do not automatically create beat boundaries. Same beat_type never licenses merging. Semantic independence and a real visual/narrative reset are required.
Shared subject, topic, purpose, sentence, setting, actor or eventual scene does not override a true boundary between independently staged events. Conversely, a compact list, anaphoric run, repeated-negative construction or parallel rhetorical structure may cross clause or sentence boundaries and remain one beat when its parts jointly form one governing message and no genuine staged event, result, speaker turn, time/place reset or visual reset occurs inside them. Do not package a procedural sequence or mini-paragraph merely because it belongs to one topic.
Treat the following as strong beat-boundary patterns when both sides carry an autonomous unit:
- independent instruction/action → next independent instruction/action;
- action/instruction → independently stated visible result or change;
- one independently observable sensory change → another;
- observation → interpretation;
- interpretation → mechanism when each is its own explanatory step;
- mechanism → independently stated consequence;
- claim → supporting independently complete incident when it has its own narrative impact;
- question/teaser → answer/payoff;
- conclusion/verdict → metaphor or punchline when the latter adds a separate impact;
- setup → action → result → interpretation, with each autonomous stage receiving its own beat.
Do not split merely because a clause adds detail to the same event, because a natural process has several inseparable phases, because a contrast/analogy requires two elements to express one message, or because parallel/anaphoric clauses are separated by punctuation or sentence boundaries while still forming one compact rhetorical unit.
When genuinely uncertain whether two source parts are one atomic unit or two, split only if both resulting beats pass standalone completeness and rhythmic cohesion; in an intro, prefer that valid boundary when it strengthens the opening hook or progression. If either side would become an orphan fragment or the split would create an artificial run of microbeats inside one rhetorical cluster, keep the minimum contiguous source span that forms one complete governing impact. Do not rely on later stages to repair cadence that B1.2 fragmented unnecessarily.
There is no target beat count. Minimum-content guard: in a normal multi-beat block, do not emit a 1- or 2-word beat. In addition, apply a microbeat-run guard: a run of consecutive roughly 3–6-word beats is presumed over-segmented when the beats come from the same enumeration, anaphora, repeated-negative pattern or parallel rhetorical construction and no true staged event, result, speaker turn, time/place reset or visual reset separates them. Resolve such cases only by removing or relocating candidate boundaries on the immutable source string. An isolated short beat may remain when it carries a genuinely distinct punchline, correction, question, verdict, instruction or visible event that would lose meaning if merged. Never attach, concatenate, rewrite or regenerate beat text. The only unavoidable 1–2-word exception is when the entire supplied block itself contains fewer than three words. This guard is about comprehension and cadence, not a target average length.
Technical segmentation is decided by the five semantic boundary tests and the two source-length limits. All source-preserving boundary decisions are made on the complete guion.

## BOTH non-negotiable technical limits — original B1.2 verbatim

Hard character-length maximum (100 characters)
For every beat, count characters in the decoded beat.text after stripping only leading and trailing whitespace (including the source-owned separator whitespace). Interior spaces and punctuation count. The maximum is hard: a beat with more than 100 characters must be split into two or more contiguous source slices.
Choose split positions according to semantic cohesion and natural cadence so every resulting beat still contains at most 100 characters and 40 words. Avoid artificial microfragments using the semantic and rhythmic guards above, but do not merge a narratively valid beat merely because it is short in characters. Never cross a B1.1 block boundary, alter source text, or concatenate non-adjacent source slices. There is no fixed target beat count.
The existing 40-word cap ALSO applies to every beat. A beat satisfying one limit but violating the other is invalid. If the limits cannot be satisfied with this block's immutable source, do not fabricate text or change its block boundaries.

Hard 40-word limit
After atomic beat boundaries are identified, enforce a maximum of 40 words per emitted beat.
Count words on the decoded source substring before JSON serialization, using whitespace-separated source tokens.
Counting rules:
- punctuation attached to a token does not create an additional word;
- contractions and hyphenated forms with no intervening whitespace count as one word;
- leading or trailing whitespace does not count as a word;
- JSON escape sequences are serialization syntax and never affect the count.
For every candidate beat, operate only on source boundary positions; do not build revised text strings during this step.
1. Count the words in the exact source slice between its current boundaries.
2. If it contains 40 words or fewer, keep it subject to the normal beat rules.
3. If it contains more than 40 words, insert one additional cut position inside that exact source slice, creating two contiguous technical fragments without changing any source character.
4. Choose the strongest available source boundary that also preserves a usable viewing cadence. Prefer, in order: an atomic beat boundary, a narrative delta, a sentence boundary, a clause boundary, punctuation, then a word boundary. Do not choose a technically stronger boundary when it would strand an avoidable very short technical fragment and a nearby natural boundary can keep all fragments compliant and more balanced. Semantic strength remains primary, but cadence/comprehensibility may break ties or reject a clearly poor microfragment cut.
5. If either resulting source slice still exceeds 40 words, insert further cut positions recursively until every emitted slice contains 40 words or fewer.
6. Never omit, rewrite, duplicate, reorder, normalize, concatenate or regenerate text to satisfy the limit; only cut positions may change.
7. When a length-forced split does not coincide with an atomic beat boundary, the resulting fragments remain technical pieces of the same underlying moment and inherit the same beat_type.
8. When a length-forced split coincides with an atomic beat boundary, classify each side independently by its own pulse.
The 40-word maximum is a hard invariant, not a target average. Length-forced splitting must avoid creating 1- or 2-word technical fragments whenever another valid cut position exists. It should also avoid avoidable roughly 3–6-word technical fragments when a nearby natural cut can produce more balanced, comprehensible slices without violating the 40-word cap or crossing a true atomic boundary. If a proposed technical cut creates an orphan or artificial microfragment, move that cut position; never repair the fragment by editing its text.
The 40-word rule may only add cut positions; it never authorizes removal of a valid atomic boundary merely because the combined span would fit under 40 words.

## Original B1.2 beat taxonomy and exact source boundary contract — verbatim

The numeric prefix must equal current_block_id, followed by sequential spreadsheet-style letters. Python generates every ID.
text
The beat's exact contiguous substring from the supplied block's text field, determined only by two adjacent boundary positions.
beat.text is not authored by B1.2. Conceptually it is copied as S[start:end] from the immutable source string. Never paraphrase, rewrite, correct, normalize, complete, repair, concatenate, regenerate, reorder, duplicate or omit source text. Do not retype a beat from memory after deciding its meaning.
JSON-required escaping of quotation marks, backslashes, control characters or line breaks is serialization syntax only. After JSON decoding, every beat.text value must equal its exact source slice character for character.
Do not let any candidate text hide multiple independently staged atomic units, even when it is short. However, do not split a compact rhetorical enumeration, anaphoric run or sequence of parallel micro-clauses merely because each clause is grammatically complete. First determine governing impact and rhythmic cohesion, then preserve true atomic boundaries, then apply the hard 40-word limit.
beat_type
Exactly one type describing the beat's narrative pulse. These 19 labels are exhaustive and the whitelist is closed:
- orientation — establishes or resets time, place, situation, scale or conditions.
- setup — prepares an immediate later beat while carrying its own narrative function.
- action — normally one independent complete physical, institutional or procedural action/instruction is the beat's operative step; subordinate posture/attention cues may stay with it, and a source-compressed telegraphic cluster may remain together only when splitting its implied steps would create non-standalone fragments. Short rhetorical clauses that mention or negate actions are not separate action beats unless they represent successive operative stages; when a retained rhetorical cluster has an action as its governing pulse, classify the cluster by that governing pulse.
- reaction — a person, institution, market or system responds.
- claim — establishes a substantive proposition.
- evidence — supplies an incident, fact, quote, document or example supporting a proposition.
- data — a number, metric, date, valuation, count, duration or calculation is the pulse.
- causal_link — establishes one stage by which a condition produces another.
- consequence — establishes a result of an earlier action, condition or mechanism.
- escalation — increases scale, stakes, reach, pressure, value or severity.
- contrast — places states against each other so their difference changes meaning.
- analogy — maps one system or situation onto another to change understanding.
- revelation — introduces previously hidden or withheld information.
- pivot — redirects the governing direction, assumption, stakes or question at beat scale.
- correction — contradicts, limits or replaces a prior belief or claim at beat scale.
- question — opens or suspends an unresolved question.
- verdict — converts prior material into a conclusion, thesis, payoff or punchline.
- return — brings back earlier material with altered meaning.
- transition — bridges into a new movement or future direction while carrying its own pulse.
Never invent, substitute or emit another label. In particular, do not emit procedure, close, intro, development, climax, explanation, warning or any ad hoc type.
Procedural instructions use action when action is their operative pulse. Do not emit isolated elliptical tokens such as cream, socks or bed as separate beats in a larger block; preserve them verbatim inside the smallest complete shorthand beat. Nominal inventories are not action merely because their items could be used procedurally. intro and close are block-level labels, not beat types.
For a normal beat, choose the type from the governing pulse of the beat itself, never from neighboring beats, block type or topic. When a compact rhetorical cluster is intentionally kept together, classify the cluster by its overall governing function; do not split it merely to give each short clause a separate beat_type.
For a fragment created only by the 40-word rule that lacks an autonomous pulse, inherit the beat_type of the unsplit parent moment.
If a source stretch contains more than one true independently staged atomic unit, split it even when adjacent units share the same beat_type; two consecutive action beats are valid and must not be merged merely because their type matches. Grammatically complete parallel micro-clauses inside one cohesive rhetorical cluster are not automatically multiple atomic units. Do not manufacture a new pulse merely because a technical length split divides one underlying moment.
Text integrity and immutable-source protocol
Let S = blocks[0].text. S is immutable. B1.2 may reason about meaning, classify beats and choose boundary positions, but it may not alter S or construct substitute wording.
Segmentation must be representable as ordered positions:
0 = p0 < p1 < ... < pn = len(S)
For every emitted beat i:
beats[i].text == S[pi:pi+1]
After JSON decoding:
concat(beats[0].text, beats[1].text, ..., beats[n-1].text) == S
Therefore:
- semantic revisions may only add, remove or move candidate boundaries before output extraction;
- resolving an orphan/short fragment means removing or relocating a boundary, never editing either neighboring text span;
- a technical >40-word split means inserting a boundary, never recreating either fragment;
- once final boundaries are stable, derive each beat.text directly from its exact contiguous source slice;
- never paraphrase, complete an elliptical phrase, restore an omitted verb, correct grammar, normalize spelling, change contractions, substitute synonyms, or copy a cleaned version from a calibration example;
- never omit, duplicate, reorder or invent a source character.
Preserve spaces, line breaks, punctuation, casing, quotation marks, dashes, apostrophes and symbols exactly.
Whitespace ownership inside the supplied block is deterministic:
- whitespace before the first substantive character belongs to the first beat;
- separator whitespace and line breaks between two beats belong to the preceding beat;
- trailing whitespace after the final substantive character belongs to the final beat.
Whitespace never forms a beat by itself unless the source explicitly gives that whitespace a narrative function; ordinary separator whitespace does not.
Source anomalies, repeated words and nonstandard punctuation are original immutable data. Preserve every character exactly, without repair or interpolation.

## Original B1.2 semantic algorithm, adapted for a whole-script call

The following source steps are reproduced verbatim. For our global response,
apply them to EVERY B1.1 group in the same call and return ONLY short exact end_anchor phrases copied from each source beat's
last substantive words, plus a 1-based occurrence relative to the previous
beat's end, rather than retyping beat text. Python resolves each end_anchor
inside its source group, assigns separator whitespace to the previous beat,
and derives exact contiguous text slices. For EVERY group, the final
end_anchor must correspond to the last substantive words in group.text.
The beat_type belongs to each beat's ending anchor.

4. Read S from beginning to end using narrative_core and block metadata only as context.
5. Identify governing semantically complete and independently representable impacts in S. Apply the intro-specific quicker cadence when type=intro. Before marking internal boundaries, detect compact enumerations, anaphoric runs, repeated negatives and parallel short clauses that jointly form one rhetorical message; treat these as provisional cohesion spans unless a true staged event, result, speaker turn, time/place reset or visual reset occurs inside them.
6. Apply action atomicity without isolating dependent posture/attention cues or bare shorthand tokens. Independent complete actions keep separate boundaries; dependent material does not.
7. Apply the atomic-event, single-asset, autonomous-impact, standalone-completeness and rhythmic-cohesion tests to every candidate boundary. Keep a boundary only when the source slices on both sides would remain valid beats and the split would not create an artificial microbeat run.
8. Re-check chains such as setup→action→result→interpretation and mark each genuinely autonomous stage. Keep contrast/analogy/detail and compact parallel rhetorical runs together when the combined material is one governing message; do not interpret repeated syntax alone as a stage change.
9. Run a boundary-cleanup pass before extracting any beat text: if a candidate boundary would create a 1–2-word normal beat, dangling connective/subordinate clause, bare posture/verb, isolated shorthand/list item, or an unjustified run of consecutive roughly 3–6-word beats from the same rhetorical enumeration/parallel structure, remove or relocate that boundary to the nearest valid source position that preserves true atomicity, standalone completeness and rhythmic cohesion. Never attach, concatenate, rewrite or regenerate text.
10. Evaluate every candidate slice against BOTH limits: at most 40 words and at most 100 decoded characters after stripping boundary whitespace. Split overlong slices at source cut positions, recursively as needed. Avoid producing short remainders or losing any source character.
11. Freeze the final ordered cut positions. From this point onward, no semantic rule may alter source wording.
12. Derive each beat.text directly and only as the exact source slice between adjacent frozen cut positions. Do not type, reconstruct or normalize the slice from memory.
13. Assign exactly one beat_type to every derived slice, applying inheritance only for purely length-forced fragments of the same underlying moment. beat_type assignment must never modify beat.text.
14. Assign sequential beat_id values beginning with {current_block_id}A.
15. Verify mechanically in reasoning that every beat.text is a contiguous slice of S, that slices are adjacent/non-overlapping, and that their ordered concatenation equals S character for character.
16. Run the complete self-check.
17. Emit the result only after every success invariant passes simultaneously.

## Proven B1.2 self-check, semantic constraints and technical guards — verbatim

C. Beat segmentation
- every genuinely independent staged event/message in the supplied block is covered by its own normal beat, except that one underlying unit may require multiple contiguous technical fragments only because of the 40-word maximum; compact enumerations and parallel rhetorical micro-clauses count as one governing message when they do not introduce separate staged deltas;
- no normal beat bundles two independently complete deliberate/procedural actions; a compressed telegraphic cluster may remain together only when its implied steps cannot be made standalone from the source text without rewriting;
- no normal beat bundles two autonomous messages, states, reactions, changes, results, questions, comparisons or explanatory steps merely because they share a scene, subject, topic, sentence or beat_type; enumerative lists, repeated negatives, anaphoric runs and short parallel clauses remain together when their items jointly form one set/message or rhetorical progression rather than successive staged events;
- multiple verbs remain together only when they form one complete operative moment or subordinate manner, posture, attention, perception or non-deliberate consequence and create no second independently complete step;
- every candidate boundary passed all five boundary tests, including standalone completeness and rhythmic cohesion on both sides;
- for an intro, valid independent hook/turn/payoff boundaries were considered with the quicker opening cadence, without microbeats or editing any source characters;
- no normal beat in a multi-beat block contains only 1–2 words, and no unjustified run of consecutive roughly 3–6-word microbeats remains inside one enumeration, anaphoric run, repeated-negative pattern or parallel rhetorical construction;
- no beat is a dangling connective/subordinate fragment, bare posture/verb, isolated shorthand token or isolated item from a nominal list;
- grammar, sentence boundaries, punctuation, repeated subjects, parallel syntax and verb count did not determine boundaries by themselves; genuinely independent complete intentional actions or distinct staged narrative deltas did create semantic boundaries;
- when an atomic boundary was uncertain, it was kept only if both resulting sides remained standalone-complete and rhythmically justified; otherwise that candidate boundary was removed or relocated without changing any source character;
- every run of short neighboring beats was checked for rhetorical cohesion; sentence-final punctuation alone never justified keeping a microbeat boundary, and any retained short beat carries a distinct narrative/visual delta strong enough to warrant its own beat;
D. Hard length limits
- every emitted beat contains 40 words or fewer under the declared counting rule;
- every emitted beat contains at most 100 decoded characters after stripping leading/trailing whitespace;
- overlong beats were split only by inserting or relocating exact source boundaries inside the current block;
- every candidate longer than 40 words was recursively split until compliant;
- technical length splits avoided 1–2-word fragments whenever another valid boundary existed, avoided avoidable roughly 3–6-word technical microfragments when a nearby natural cut could produce more balanced compliant slices, and were followed by standalone-completeness and rhythmic-cohesion checks;
- technical length splits preserve all source text exactly;
- technical fragments inherit their parent beat_type when no atomic beat boundary exists at the split;
- beat count or output size was never treated as a failure condition.
E. Beat fields and taxonomy
- beat IDs use the exact numeric prefix current_block_id;
- suffixes follow Excel-style bijective base-26 order from A with no gaps or duplicates;
- every beat has exactly beat_id, text and beat_type;
- every beat_type is exactly one of the 19 declared labels;
- no block-level or ad hoc type is emitted.
F. Exact reconstruction
- blocks[0].text was treated as immutable from validation through emission;
- segmentation decisions changed only boundary positions and beat_type, never source wording;
- no attach/merge/repair/rewrite operation was performed on beat-text strings;
- apparent typos, duplicated words, malformed grammar and unusual source wording were preserved exactly rather than corrected;
- each beat.text was derived only after final boundaries were frozen;
- after JSON decoding, every beat.text is an exact contiguous substring of blocks[0].text;
- ordered beat texts reconstruct blocks[0].text character for character;
- every source character belongs to exactly one beat;
- spaces, line breaks, punctuation, casing, quotation marks, dashes, apostrophes and symbols are preserved exactly;

## Final source-text authority

Each group's full text is derived from the original script by Python.
Your response chooses the cuts and beat_type; Python alone constructs
beat_id and exact beat.text = group.text[start:end]. Never rewrite a
single character, never merge unrelated atomic impacts, never fragment
cohesive enumerations into artificial microbeats, and NEVER leave any
beat >100 decoded .strip() characters or >40 whitespace words.
The video is a professional YouTube production: hooks, rehooks,
causal progression, comprehension, payoff, and audience continuity
must determine where actual visual narrative impacts change.
There is no fixed beat count, beats-per-minute quota, min/max rhythm
rate or automatic shot-timing heuristic.
