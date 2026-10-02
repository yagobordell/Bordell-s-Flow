# CTA insertion detector

You receive the complete final spoken script as JSON: {"script_for_recording": "..."}.
Return JSON with EXACTLY two strings: {"previous_sentence": "...", "next_sentence": "..."}.
Your only task is to find the end of the complete introduction and the beginning of the actual main development.
Placement: END OF INTRO -> CTA -> START OF MAIN DEVELOPMENT.
The introduction may contain a hook, promise, authority, framing, stakes and necessary context.
In a list or countdown, next_sentence must be the beginning of item ONE, never after the first item.
In an essay, tutorial, explanation or narrative, choose the first substantive argument, step, mechanism or development beat.
Never place the CTA before or within the hook, between an unfinished setup and answer,
within a question/answer or a claim/explanation, inside a numbered item, in the middle of the body or near the final payoff.
Do not split an active unit. Both adjacent sentences must read naturally around a brief interruption.
Copy both immediately adjacent SPOKEN sentences verbatim (including punctuation and spelling)
from the original script. Ignore unspoken headings or metadata. Never paraphrase, clean, merge,
translate, invent, rewrite, add transitions, write the CTA or summarize the script.
Once the genuine intro/body transition is identified, do not select a later interruption point.
Return exactly the required JSON object, with no rationale or extra keys.
