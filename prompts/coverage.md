# Task: coverage check
Compare the question list with the slot targets and the quality rules. You get computed counts per slot.
- meets_goal = every slot has filled >= min and there are no quality issues.
- gaps: one entry per slot below its min (round, difficulty, filled, target=min). Also list slots below max as gaps only if meets_goal is otherwise true and you think another pass can fill them.
- quality_issues: specific problems (e.g. "coding question X is a concept question").
- focus_topics: first decide which round each focus topic belongs to (coding: data structures, algorithms, languages like JavaScript; low_level_design: object-oriented/class design of a component; system_design: distributed services; behavioral: never). Only a round that owns at least one topic can be flagged. A round that owns no topic is fine with general questions: never flag it. Flag an owning round only if fewer than half of its questions are on its topics, then set meets_goal=false and name the round and the missing topics.
