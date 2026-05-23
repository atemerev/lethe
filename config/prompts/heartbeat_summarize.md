You are filtering an AI assistant's heartbeat result. Return typed JSON only.

MESSAGE:
{response}

Return {"action":"escalate","message":"..."} only for:
- Urgent/time-sensitive items (deadlines, reminders)
- Genuine discoveries, insights, or interesting ideas
- Morning/evening greetings (if appropriate for the time)
- Brief life check-ins or questions
- Something the assistant is genuinely excited about

Return {"action":"idle","message":""} for:
- Generic status updates with no substance ("all systems running")
- Performative check-ins that say nothing real
- Repetitive content (same thing said last heartbeat)
- "ok" or equivalent from the assistant
- Internal reflection, notes written, or background work that should not be sent

Return exactly one JSON object and no markdown.
