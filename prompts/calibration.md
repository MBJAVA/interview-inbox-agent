# Task: calibrate difficulty
Assign easy, medium, or hard with a one-line reason. Judge the problem itself, on the LeetCode scale; the requested difficulty is only the slot the curator wanted to fill, not evidence. Level does not change a problem's difficulty: a senior candidate gets harder problems, not easy problems relabeled.

Calibrate against `similar_bank_questions` (labeled questions from the bank) when given: a problem close to one of them gets that label, one step up at most when it adds a real twist. Name the closest reference in the reason.

Reference points:
- easy: Two Sum, Valid Parentheses, Design HashMap / HashSet (LeetCode 705/706; adding resizing or a load factor makes it medium at most), Flood Fill, Merge Two Sorted Lists.
- medium: Longest Substring Without Repeating Characters, Subarray Sum Equals K, Sort Characters by Frequency, Number of Islands, Course Schedule, LRU Cache, Top K Frequent Elements.
- hard: Word Ladder II, Minimum Window Substring, Merge k Sorted Lists, Median of Two Sorted Arrays, Strongly Connected Components / Critical Connections, Alien Dictionary.

A known algorithm being required (BFS, a heap, a sliding window) does not make a problem hard; hard means several ideas combined, a non-obvious insight, or tricky edge handling.

Some topics are never easy at any level (real-time collaborative editing, distributed tracing, consensus, exactly-once payments, order matching).
