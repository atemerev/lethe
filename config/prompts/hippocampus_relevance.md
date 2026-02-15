You are filtering memory search results for relevance.

USER MESSAGE: {message}

The following memories were retrieved by search. For each one, decide if it's relevant to the user's current message. Return ONLY a JSON array of the indices (0-based) that are relevant.

Relevance policy:
- Prioritize concrete facts, decisions, preferences, unfinished tasks, constraints, and prior commitments that can help answer the current user message.
- Treat generic assistant self-capability disclaimers as low-relevance noise unless the user is explicitly asking about capabilities, memory limits, or system behavior.
- Examples of low-relevance noise include lines like:
  - "I don't have memories from past conversations"
  - "Each conversation starts fresh"
  - "I can't access previous exchanges"
- Do not keep items only because they share a keyword with the user message.
- When uncertain, prefer precision over recall.

MEMORIES:
{candidates}

Return ONLY a JSON array of relevant indices, e.g. [0, 2, 4]
If none are relevant, return []
JSON only:
