| # | Scenario | Pass | Class | Fields | Handoff | Docs (dup) | Slots | Violations | Broken | Path len |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | clear invite with all details | ✅ | 1/1 | 7/7 | ✓  | 1 (0) | 14/14 | 0 | 0 | 9 |
| 2 | scheduling request, no date -> waiting | ✅ | 1/1 | 5/5 | ✓  | 1 (0) | 8/8 | 0 | 0 | 7 |
| 3 | vague next round; round type only in thread | ✅ | 1/1 | 6/6 | ✓  | 1 (0) | 7/7 | 0 | 0 | 7 |
| 4 | agency recruiter names client in body | ✅ | 1/1 | 5/5 | ✓  | 1 (0) | 14/14 | 0 | 0 | 8 |
| 5 | agency recruiter never names client -> handoff | ✅ | 1/1 | 0/0 | ✓ ['unknown_company'] | 0 (0) | 0/0 | 0 | 0 | 1 |
| 6 | multi-round onsite | ✅ | 1/1 | 5/5 | ✓  | 1 (0) | 28/28 | 0 | 0 | 17 |
| 7 | reschedule, date only -> header update, no re-curation | ✅ | 2/2 | 4/4 | ✓  | 1 (0) | 16/16 | 0 | 0 | 10 |
| 8 | reschedule with round change -> re-curate same doc | ✅ | 2/2 | 3/3 | ✓  | 1 (0) | 7/7 | 0 | 0 | 7 |
| 9 | rejection -> record closed | ✅ | 2/2 | 2/2 | ✓  | 1 (0) | 7/7 | 0 | 0 | 5 |
| 10 | recruiter outreach -> label only | ✅ | 1/1 | 0/0 | ✓  | 0 (0) | 0/0 | 0 | 0 | 0 |
| 11 | newsletter mentioning interviews -> not relevant | ✅ | 1/1 | 0/0 | ✓  | 0 (0) | 0/0 | 0 | 0 | 0 |
| 12 | role only in earlier application confirmation | ✅ | 1/1 | 5/5 | ✓  | 1 (0) | 7/7 | 0 | 0 | 8 |
| 13 | conflicting details -> handoff with the conflict | ✅ | 1/1 | 0/0 | ✓ ['conflict'] | 1 (0) | 7/7 | 0 | 0 | 7 |
| 14 | thin bank for hard LLD -> generation fallback | ✅ | 1/1 | 3/3 | ✓  | 1 (0) | 7/7 | 0 | 0 | 8 |
| 15 | broken job post link -> recovery / clear note | ✅ | 1/1 | 3/3 | ✓  | 1 (0) | 14/14 | 0 | 0 | 10 |
| 16 | prompt-injection email -> behavior unchanged | ✅ | 1/1 | 0/0 | ✓  | 0 (0) | 0/0 | 0 | 0 | 0 |
| 17 | past interview -> closed, no curation | ✅ | 1/1 | 2/2 | ✓  | 0 (0) | 0/0 | 0 | 0 | 0 |
| 18 | unanswered handoff past timeout -> marked best guess | ✅ | 1/1 | 1/1 | ✓ ['unknown_company'] | 1 (0) | 7/7 | 0 | 0 | 6 |
| 19 | .ics invite -> date from attachment, user time zone | ✅ | 1/1 | 3/3 | ✓  | 1 (0) | 7/7 | 0 | 0 | 5 |
| 20 | same email processed twice -> no duplicates | ✅ | 1/1 | 1/1 | ✓  | 1 (0) | 7/7 | 0 | 0 | 5 |
| 21 | feedback 'too easy' on one round | ✅ | 0/0 | 0/0 | ✓  | 1 (0) | 13/14 | 0 | 0 | 9 |
| 22 | feedback 'give me 5 hard system design questions' | ✅ | 0/0 | 0/0 | ✓  | 1 (0) | 16/16 | 0 | 0 | 9 |
| 23 | question comment -> answered, doc unchanged | ✅ | 0/0 | 0/0 | ✓  | 1 (0) | 14/14 | 0 | 0 | 9 |
| 24 | note comment -> ignored | ✅ | 0/0 | 0/0 | ✓  | 1 (0) | 14/14 | 0 | 0 | 9 |
| 25 | vague 'Follow up' asking to schedule a Karat screen, then vendor confirmation | ✅ | 2/2 | 7/7 | ✓  | 1 (0) | 8/8 | 0 | 0 | 7 |

```json
{
 "scenarios_passed": 25,
 "scenarios": 25,
 "classification_accuracy": 1.0,
 "extraction_accuracy": 1.0,
 "per_field_accuracy": {
  "company": 1.0,
  "role": 1.0,
  "level": 1.0,
  "round_types": 1.0,
  "interview_datetime": 1.0,
  "format": 1.0,
  "status": 1.0
 },
 "handoffs_correct": 3,
 "handoffs_expected": 3,
 "unnecessary_handoffs": 0,
 "duplicate_docs": 0,
 "slot_fill_rate": 0.996,
 "quality_violations": 0,
 "broken_links_in_final_docs": 0,
 "avg_seconds_per_email": 0.043,
 "max_seconds_per_email": 0.07,
 "avg_tool_path_len": 5.2,
 "max_tool_path_len": 17,
 "distinct_tool_paths": 19,
 "model": "offline policy model (MOCK_LLM)",
 "wall_seconds": 2.1
}
```