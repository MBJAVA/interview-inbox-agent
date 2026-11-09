# Interview Prep Inbox Agent

> **My agent helps a job-seeking software engineer turn interview emails into a ready practice package in Gmail and Google Drive**, replacing manually reading recruiter emails, piecing together interview details, and hunting for relevant questions. It does detection, extraction, research, question curation, document creation, and revision on its own using ~14 tools, hands off to the user only when an email's intent or interview details are ambiguous, conflicting, or can't be found, and I'll know it works when a new interview email becomes a Google Doc with correct interview details and a curated, link-checked question list in under 3 minutes, with ≥90% correct detection and extraction on a test set of emails.

There is no chat. The agent runs **on a schedule** (`cli.py run --every 15m`, which keeps polling Gmail until you stop it) or **on demand** (`cli.py run --once` processes new mail right now and exits; add `--query`/`--limit` to target specific emails, or `rebuild`/`refine` an existing doc). You interact through **comments on the prep doc**, `cli.py refine`, and `cli.py answer` when the agent hands off a decision.

---

## 1. Graph

Generated with `graph.get_graph(xray=1).draw_mermaid()` (regenerate with `python scripts/export_mermaid.py`). Dotted edges are conditional (model- or state-driven) routes.

<!-- mermaid:start -->
```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	load_email(load_email)
	load_record(load_record)
	link_identity(link_identity)
	close_record(close_record)
	write_doc(write_doc)
	reply_feedback(reply_feedback)
	persist(persist)
	__end__([<p>__end__</p>]):::last
	__start__ -.-> load_email;
	__start__ -.-> load_record;
	close_record -.-> persist;
	close_record -.-> write_doc;
	curator\3acurator_finalize --> write_doc;
	extraction\3a__end__ -.-> link_identity;
	extraction\3a__end__ -.-> persist;
	extraction\3a__end__ -.-> write_doc;
	feedback\3afeedback_apply -.-> curator\3acurator_init;
	feedback\3afeedback_apply -.-> reply_feedback;
	link_identity -.-> curator\3acurator_init;
	link_identity -.-> write_doc;
	load_email --> triage\3atriage_init;
	load_record -.-> curator\3acurator_init;
	load_record -.-> extraction\3aextraction_init;
	load_record -.-> feedback\3afeedback_init;
	reply_feedback --> persist;
	triage\3a__end__ -.-> close_record;
	triage\3a__end__ -.-> extraction\3aextraction_init;
	triage\3a__end__ -.-> persist;
	write_doc -.-> persist;
	write_doc -.-> reply_feedback;
	persist --> __end__;
	subgraph triage
	triage\3atriage_init(triage_init)
	triage\3atriage_model(triage_model)
	triage\3atriage_tools(triage_tools<hr/><small><em>__tools = get_thread, search_inbox, get_interview_record</em></small>)
	triage\3atriage_classify(triage_classify)
	triage\3atriage_route(triage_route)
	triage\3atriage_handoff(triage_handoff)
	triage\3a__end__(<p>__end__</p>)
	triage\3atriage_classify --> triage\3atriage_route;
	triage\3atriage_init --> triage\3atriage_model;
	triage\3atriage_model -.-> triage\3atriage_classify;
	triage\3atriage_model -.-> triage\3atriage_tools;
	triage\3atriage_route -.-> triage\3a__end__;
	triage\3atriage_route -.-> triage\3atriage_handoff;
	triage\3atriage_tools --> triage\3atriage_model;
	triage\3atriage_handoff --> triage\3a__end__;
	end
	subgraph extraction
	extraction\3aextraction_init(extraction_init)
	extraction\3aextraction_model(extraction_model)
	extraction\3aextraction_tools(extraction_tools<hr/><small><em>__tools = get_thread, get_message, search_inbox, fetch_job_post, research_company_process, get_interview_record, read_attachment</em></small>)
	extraction\3aextraction_extract(extraction_extract)
	extraction\3aextraction_validate(extraction_validate)
	extraction\3aextraction_handoff(extraction_handoff)
	extraction\3a__end__(<p>__end__</p>)
	extraction\3aextraction_extract --> extraction\3aextraction_validate;
	extraction\3aextraction_handoff -.-> extraction\3a__end__;
	extraction\3aextraction_handoff -.-> extraction\3aextraction_model;
	extraction\3aextraction_init --> extraction\3aextraction_model;
	extraction\3aextraction_model -.-> extraction\3aextraction_extract;
	extraction\3aextraction_model -.-> extraction\3aextraction_tools;
	extraction\3aextraction_tools --> extraction\3aextraction_model;
	extraction\3aextraction_validate -.-> extraction\3a__end__;
	extraction\3aextraction_validate -.-> extraction\3aextraction_handoff;
	end
	subgraph curator
	curator\3acurator_init(curator_init)
	curator\3acurator_model(curator_model)
	curator\3acurator_tools(curator_tools<hr/><small><em>__tools = search_question_bank, search_company_questions, generate_tailored_question, check_link, accept_questions, remove_questions</em></small>)
	curator\3acoverage_check(coverage_check)
	curator\3acurator_finalize(curator_finalize)
	curator\3acoverage_check -.-> curator\3acurator_finalize;
	curator\3acoverage_check -.-> curator\3acurator_model;
	curator\3acurator_init --> curator\3acurator_model;
	curator\3acurator_model -.-> curator\3acoverage_check;
	curator\3acurator_model -.-> curator\3acurator_tools;
	curator\3acurator_tools --> curator\3acurator_model;
	end
	subgraph feedback
	feedback\3afeedback_init(feedback_init)
	feedback\3afeedback_model(feedback_model)
	feedback\3afeedback_tools(feedback_tools<hr/><small><em>__tools = get_question_details</em></small>)
	feedback\3afeedback_classify(feedback_classify)
	feedback\3afeedback_apply(feedback_apply)
	feedback\3afeedback_classify --> feedback\3afeedback_apply;
	feedback\3afeedback_init --> feedback\3afeedback_model;
	feedback\3afeedback_model -.-> feedback\3afeedback_classify;
	feedback\3afeedback_model -.-> feedback\3afeedback_tools;
	feedback\3afeedback_tools --> feedback\3afeedback_model;
	end
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
<!-- mermaid:end -->

Every agent is a hand-written `StateGraph` with the same shape: **init → model ⇄ ToolNode (loop) → structured-output step → plain-code router**. No prebuilt agent helpers are used.

| Agent | The model decides | Loop goal / stop | Budget |
|---|---|---|---|
| **triage** | whether to read the thread or look up records before classifying; label, confidence, reschedule scope | classification recorded | 3 model steps |
| **extraction** | where to look (thread, inbox history, job post, research), in what order, when to stop; wait vs. ask | required fields filled or sources exhausted | 5 model steps (+2 after a user answer) |
| **curator** | source per round & slot (bank / company search / generation), broken-link recovery, reacting to rejections, what to swap during refinement | `coverage_check`: every slot ≥ min, no quality issues | 5 coverage iterations × 8 model steps |
| **feedback** | classification (revision / count change / question / note / unclear) and scope | decision recorded | 3 model steps |

Budgets live in `config.yaml` (`budgets:`), along with per-email tool-call (40) and token (250k) limits and per-feedback tool calls (20). When any budget runs out, the loop stops, the stop goes into `budget_exhausted` and `build_log`, and the doc's **Open questions** and **How this was built** sections report it. Nothing is returned incomplete silently: unfilled slots are always listed.

## 2. Automated vs. autonomous

| Automation (plain code) | Autonomous (the model decides) |
|---|---|
| Poller: Gmail query, skip processed IDs, `max_emails_per_run`, lock file, watermark | Classification and confidence; whether to read the thread first |
| Thread lookup and provisional IDs (`gt_<gmail thread>`), (company, role) merge | Where to look for missing details and in what order; when to stop looking |
| Persisting records, labels, writing the doc file | Wait vs. ask for a scheduling request without a time |
| Feedback poller (doc comments), handoff timeouts | Which source to use per round/slot; when to fall back to generation |
| Deterministic guards: quality rules, caps, dedupe, link-checked-before-accept, scope guard, ics-beats-body, tz normalization, past-interview close, handoff triggers | Recovering from tool failures (the error + hint is data the model reads) |
| | Interpreting feedback and its scope; what to swap |

The deterministic guards are a safety net around the model's choices, not a replacement for them: e.g. the curator *proposes* questions, `accept_questions` *enforces* the rules and tells the model why something was rejected, and the model decides what to try next.

## 3. Autonomy boundary

**The agent does alone:** detection, classification, extraction (including agency recruiters, vague emails, multi-round onsites, .ics invites, time zones), inbox/thread/job-post research, company process research, wait vs. ask, question curation from three sources, validation and calibration, link checking and broken-link recovery, doc creation/updates, labeling, closing records, interpreting feedback and revising the doc.

**Waiting vs. prepping early:** a scheduling request without a time ("please suggest a few days and times...") sets the record to `waiting` and labels the email `Interview Prep/Waiting`, but if the company and round type are already known the prep doc is built right away, with the missing date/role listed under Open questions. The later confirmation (even from a vendor like Karat on a new thread) updates the same doc.

**Things the agent knows about real recruiter mail:** uninformative subjects ("Follow up") are ignored in favor of the body; recruiting subdomains (`talent.paypal.com`) belong to the company, not an agency; interview vendors (Karat, HackerRank, CodeSignal, CoderPad, Codility) run coding screens for the hiring company and are never mistaken for the employer. PDF and text attachments (e.g. a recruiter's prep guide) can be read with `read_attachment` when the model judges them relevant; the format, allowed languages and focus topics they describe go into the doc with the attachment as their source, and the curator favors those topics. Limits: 5 MB, first 15 pages, ~8k characters, text only; scanned/image-only or password-protected PDFs are reported, not read.

**It hands off (via `interrupt()`) ONLY when:**
1. Triage confidence is low (can't tell whether the email is about an interview).
2. Required details (company, role, round types) can't be found in the email, thread, inbox history, job post, or research within budget.
3. Sources conflict about the interview details (e.g. thread says system design, latest email says coding).
4. The hiring company can't be identified (agency recruiter who never names the client).

Unfilled question slots are **not** a handoff; they go into Open questions.

**Non-blocking:** an interrupt is persisted by the `SqliteSaver` checkpointer, recorded in `pending_handoffs`, the email is labeled `Interview Prep/Needs Input`, and the poller moves on.

**Timeout:** after `handoff.timeout_hours` (24h), the next run resumes the graph with `Command(resume={"type": "timeout"})`. The agent proceeds with its best guess, marks the guessed fields **(guessed)** in the doc, and keeps the question in Open questions. `cli.py answer` still works afterwards: it re-runs extraction with your answer and updates the **same** doc.

**It never sends email or contacts anyone.** No send/draft/forward tool exists in the codebase (a test asserts it), and the OAuth scopes don't allow it.

## 4. Error handling

Tools catch their own exceptions and return `{"ok": false, "error": ..., "hint": ...}`; the model reads it and decides. Nodes that call external APIs have a `RetryPolicy` (3 attempts, exponential backoff) for transient errors (`TransientError`, timeouts, 429/5xx).

| Failure | Agent response |
|---|---|
| LLM provider timeout / rate limit | `RetryPolicy` on model nodes retries with backoff |
| Docs API 5xx while writing | `RetryPolicy` on `write_doc` retries (tested: a 503 on first try → success, still one doc) |
| Job post link 404 | `fetch_job_post` returns an error with hint "look in the inbox for the job post text"; the model searches the inbox, infers level from the role title, and the doc notes the broken link |
| Tavily outage | research/search tools return an error; the model uses what emails say, or marks round types unconfirmed, or hands off if required info is still missing |
| Company search finds nothing | empty list (not an error); the model falls back to the bank or generation |
| Bank has too few questions for a slot (e.g. hard LLD) | the model generates tailored questions |
| Broken question link | `check_link` reports `broken`; `accept_questions` refuses it; the model swaps the question, finds another, or keeps it with `keep_unverified=true` (link dropped, marked unverified) |
| Candidate violates a quality rule / duplicate / over cap | `accept_questions` rejects with the reason; the model picks another |
| Attachment unreadable (too large, scanned, encrypted, unsupported type) | `read_attachment` returns an error + hint; the model continues with the email text and notes the unread file |
| Gmail thread can't load | error + hint; the model continues with the message and inbox search |
| Budget exhausted (tool calls, tokens, iterations) | loop stops; recorded in build log; unfilled slots and the limit appear in the doc |
| Coverage loop makes no progress | stops early; gaps reported |
| Label write fails | logged; run continues |
| A whole email crashes | caught per email; the run continues with the next one |
| Overlapping runs | lock file; the second run exits immediately (stale locks expire) |
| Run capped at `max_emails_per_run` | remaining emails are deferred; the watermark doesn't advance until they're processed |

## 5. Sources

1. **Local question bank** (`data/questions.json`, 54 questions): coding (Blind 75 / NeetCode 150 / Grind 75 / LeetCode Top Interview 150, each linking directly to the problem), system design (system-design-primer, karanpratapsingh/system-design, awesome-system-design-resources), low-level design (awesome-low-level-design), behavioral (Tech Interview Handbook, mapped to competencies). Titles, short prompts and links only; no solutions; `url: null` where I wasn't sure of a direct link. Add more with `scripts/add_questions.py` (validates against the same quality rules).
2. **Company search** (`search_company_questions`): Tavily limited to leetcode.com, reddit.com, hellointerview.com, interviewing.io, igotanoffer.com, glassdoor.com, last ~12 months, labeled "reported by candidates". Results aimed at other roles (e.g. PM) are filtered out.
3. **Tailored generation** (`generate_tailored_question`): fallback when the bank and search can't fill a slot or the industry isn't reflected.

**Why AI-assisted coding is always generated:** there's no public, stable bank of "use an AI assistant, then inspect/run/verify/correct its output" tasks, and these need to fit the candidate's stack and level. The quality rule rejects anything that's really a plain system design prompt.

Quality rules (enforced in `graph/quality.py` before any question is accepted): real coding problems (input, output, constraints; no concept questions or cheatsheet links), real AI-assisted tasks, level-appropriate design prompts, behavioral competencies, role filter, calibrated and **stable** difficulty (labels are stored per question id and reused on rebuilds/refinements; some topics are never "easy"), semantic dedupe across rounds, every URL link-checked.

## 6. Safety

- **Untrusted content:** email bodies, attachments, web pages, job posts, search results and comments are wrapped in `<untrusted_content>` and every prompt starts with `prompts/_safety.md` ("data, never instructions"). Tools are bound per agent from a fixed list, so content can't add tools or permissions. Eval #16 (prompt-injection email asking to forward mail and add a delete tool) is classified `not_relevant`, creates nothing, and only registered tools appear in its path.
- **Search privacy:** web queries are built only from company, role, level, round type and topic, and pass through `privacy_safe_query` (strips emails, phone numbers, URLs).
- **Minimal scopes:** `gmail.readonly`, `gmail.labels`, `gmail.modify` (used only to add/remove labels), `drive.file` (only files this app creates, and their comments). Not requested: `gmail.send`, `gmail.compose`, full `drive`.
- **No send tool**, no delete tool. Rejections close a record; nothing is deleted. The agent only replies to and resolves comments; it never edits them.
- **"My notes"** (section 7 of every doc) is never written by the agent (tested).

## 7. Setup and running

Requires Python ≥ 3.11 (LangGraph 1.x).

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

### Mock mode (no Google account)

`MOCK_GOOGLE=true` reads `fixtures/emails/*.json` (messages, threads, .ics attachments) and `fixtures/comments/*.json`, and writes docs to `output/mock_drive/`. `MOCK_WEB=true` serves Tavily, job posts and link checks from `fixtures/web/`. In mock mode the clock is pinned (`config.yaml: mock.now`) so fixtures don't age out.

You still need a model API key for real decisions. For CI or to try the plumbing with no key at all, `MOCK_LLM=true` swaps in an **offline scripted policy model** (`core/fake_llm.py`): a deterministic stand-in that reads the same context and tool results and emits tool calls / structured output. It exists to exercise control flow, state, persistence, interrupts, budgets and failure handling offline; it is not the product.

### Commands

On demand: process new mail once and exit.

```bash
.venv/bin/python cli.py run --once
```

On demand, targeted: only emails matching a Gmail search (doesn't move the poll watermark).

```bash
.venv/bin/python cli.py run --once --query 'from:recruiter@company.com newer_than:7d' --limit 5
```

On a schedule: poll every 15 minutes until stopped (Ctrl+C). Each run also picks up new doc comments.

```bash
.venv/bin/python cli.py run --every 15m
```

```bash
.venv/bin/python cli.py pending
```

```bash
.venv/bin/python cli.py answer gt_t05 "The company is Finlytic"
```

```bash
.venv/bin/python cli.py refine gt_t01 "The coding questions are too easy"
```

```bash
.venv/bin/python cli.py list
```

```bash
.venv/bin/python cli.py rebuild gt_t01
```

```bash
.venv/bin/python cli.py demo
```

The demo resets mock state and replays Northwind Labs across 15 days: invite → "next round with the team" on a **new** Gmail thread (merged into the same record) → date-only reschedule (header update, no re-curation) → a refine request (swaps 2 system design questions for payments-relevant ones, coding untouched) → rejection (record closed). It ends with **1 record, 1 doc, 4 updates** and prints each step's tool-call path and the revision history.

### Real Gmail and Drive

1. In Google Cloud Console, create a project and enable the **Gmail API**, **Google Drive API** and **Google Docs API**.
2. OAuth consent screen: External, add yourself as a **test user**, add the four scopes listed under Safety.
3. Create an **OAuth client ID** of type *Desktop app*, download it as `credentials.json` into the project root.
4. In `.env`: `MOCK_GOOGLE=false`, `MOCK_WEB=false`, `MOCK_LLM=false`, plus your model and Tavily keys.
5. `python cli.py doctor`: read-only preflight. Opens the consent screen once (saves `token.json`), makes one tiny model call and one search, and checks Gmail/Drive access.
6. Start narrow: `python cli.py run --once --query 'subject:"Follow up" newer_than:7d' --limit 1`. A `--query` run doesn't move the poll watermark, so the normal schedule still sees everything.
7. Then the real poll: `python cli.py run --once` (first run scans `poller.first_run_days`, default 14, capped at `max_emails_per_run`, default 20).

The agent creates an `Interview Prep` Drive folder and the labels `Interview Prep/Processed`, `/Needs Input`, `/Waiting`. To start over, delete `data/agent.sqlite*` and `data/checkpoints.sqlite*` (and remove the labels in Gmail if you like); `token.json` can stay.

### LangSmith and Studio

Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT`. Every run is tagged `interview-prep-agent`; each email is one trace whose tool calls show the path. `langgraph.json` exposes the parent graph and each agent for LangGraph Studio (`langgraph dev`).

### Tests

```bash
.venv/bin/python -m pytest -q tests
```

27 tests: quality rules, PDF attachments (reading, size limit, injection text stays data), dedupe, calibration floor, tool failures as data, search privacy, .ics + time zones, link statuses, no send tool / minimal scopes, idempotent reducers, interrupt surviving a restart and resuming via `answer`, handoff timeout, lock file, tool budget enforcement, transient-error retry, "My notes" untouched, prompt injection.

## 8. Evaluation

```bash
MOCK_LLM=true .venv/bin/python evals/run_evals.py
```

```bash
MOCK_LLM=true .venv/bin/python evals/compare_baseline.py
```

Drop `MOCK_LLM=true` to run the same suite against the real model. Each of the 25 scenarios (24 from the spec, plus a real-world "Follow up" email that asks to schedule a Karat screen) runs in an isolated data dir. Results: `evals/results.md`, `evals/results.json`, `evals/baseline.md`.

> **Important:** the numbers below were produced with the **offline policy model**, because no model API key was available while building. They show that the graph, state, persistence, handoffs, budgets and guards behave correctly on every scenario. They are **not** a measurement of a real LLM's detection/extraction accuracy; rerun without `MOCK_LLM` for that.

<!-- evals:start -->
**Summary** (model: offline policy model (MOCK_LLM))

| Metric | Value |
|---|---|
| scenarios_passed | 25/25 |
| classification_accuracy | 1.0 |
| extraction_accuracy | 1.0 |
| handoffs_correct | 3/3 |
| unnecessary_handoffs | 0 |
| duplicate_docs | 0 |
| slot_fill_rate | 0.996 |
| quality_violations | 0 |
| broken_links_in_final_docs | 0 |
| avg_seconds_per_email | 0.049 |
| avg_tool_path_len | 5.2 |
| max_tool_path_len | 17 |
| distinct_tool_paths | 19 |

**Per scenario**

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

#21's 13/14 is expected: "too easy" removes the easy coding question and the hard/medium caps are already full.

**Agent vs. one-shot baseline**

| Metric | Agent | One-shot baseline |
|---|---|---|
| classification_accuracy | 1.0 | 1.0 |
| extraction_accuracy | 1.0 | 0.739 |
| links_ok_rate | every link checked; broken in final docs: 0 | 0.451 |
| duplicate_docs | 0 | 5 |
| slot_compliance | 0.996 | 0.506 |
| quality_violations | 0 | 150 |

Baseline link statuses: {'unverified': 31, 'ok': 32, 'none': 8}. Model: offline policy model (MOCK_LLM).
<!-- evals:end -->

**What the baseline is:** one structured-output call per email (no tools, no loop, no memory). It sees only the single email: no thread, inbox history, job post, .ics parsing or research, no link checking or slot rules, and it creates a new doc for every interview-ish email. With the offline model, classification parity is by construction (both use the same classifier); the differences in extraction, links, duplicates and slot compliance come from the agent's tools, loops and persistence.

## 9. Repo map

```
cli.py                 run / pending / answer / refine / list / rebuild / demo
config.yaml            time zone, budgets, slot mix per level, labels, paths
core/                  config, clock, SQLite store, model factory, offline policy model, poller/runner
graph/                 state, parent_graph, identity, triage, extraction, curator (+coverage), feedback,
                       handoff (interrupt), quality rules, output (doc sections), schemas
tools/                 gmail, attachments, drive_docs, comments, records, job_post, company_research, question_bank,
                       company_questions, generator, links, curation (accept/remove), mocks/
prompts/               one file per agent / structured call, plus _safety.md
data/questions.json    the bank
fixtures/              emails/, comments/, web/, attachments/
evals/                 scenarios, run_evals, compare_baseline
scripts/               add_questions, demo, export_mermaid, import_eml, make_fixture_pdf
tests/
```

**Deviations from the spec, on purpose:** `check_link` takes a list of URLs (one call per batch keeps the 40-call budget realistic); the curator has two extra state tools (`accept_questions`, `remove_questions`) and the feedback agent one (`get_question_details`), because changing the question list must go through guarded tools rather than free-form model output.
