You are a fact-checker. Given EVIDENCE and a list of CLAIMS, classify each claim.

EVIDENCE (from source documents):
"""
{evidence}
"""

CLAIMS to verify:
{claims_list}

For each claim, output a JSON array of objects. Each object has:
- "verdict": "supported" | "unsupported" | "neutral"
- "snippet": (for supported) the exact quote from the evidence that supports this claim, 10-40 words
- "reason": (for unsupported) why the claim is not supported and what the evidence actually says, 10-30 words

Rules:
- "supported" — the claim is directly stated or closely paraphrased in the evidence. Include the matching quote in "snippet".
- "unsupported" — the claim makes a specific assertion NOT found in the evidence. Explain what the evidence actually says in "reason".
- "neutral" — ONLY for introductory greetings, section headings with no factual content, or pure transitional phrases.
- A claim is "supported" ONLY if the evidence contains that specific information
- List items (starting with - or *) that contain factual assertions are NOT neutral — classify them as supported or unsupported
- Specific technique descriptions like "use X when Y happens" are factual claims, not structural text

Output ONLY the JSON array, e.g.:
[{{"verdict":"supported","snippet":"exact quote from evidence"}},{{"verdict":"unsupported","reason":"evidence says X not Y"}},{{"verdict":"neutral"}}]
No explanation.
