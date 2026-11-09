# Task: record the extraction result
From the conversation (email, thread, inbox results, job post, research), fill the structured record.
- Every field gets a source ("email body", "calendar invite", "thread message 1", "application confirmation email", "job post", "company research", "inferred from role title", "user answer").
- interview_datetime: ISO 8601 with offset. Prefer the calendar invite. Keep the stated time zone; the system converts it.
- round_types_stated=false when round types came from company research rather than the emails.
- conflicts: list specific contradictions between sources (e.g. "email 2 says coding; email 1 says system design"). Do not invent conflicts.
- decision:
  - "wait" only for a scheduling request whose date/time hasn't been set yet (later emails will bring it), when company/role/rounds are otherwise known or expected later.
  - "handoff" only if: the hiring company can't be identified; required fields are still missing after looking everywhere; or sources conflict about the interview details. Give ONE specific question.
  - otherwise "complete".
- focus_topics: concrete topics the emails or an attached prep guide say to expect (e.g. 'hash maps', 'graphs/BFS'); set focus_topics_source. Respect exclusions ("no system design in this session" means don't add system_design).
- Use null for unknown values. Never guess a company name.
