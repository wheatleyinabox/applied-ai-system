# Model Card: PawPal Task Assistant

## System summary

| | |
|:--|:--|
| **What it is** | An agentic assistant added to PawPal. The owner types what their pets need in plain English, the assistant adds tasks, builds a daily schedule, checks its own work and revises it. |
| **Model** | `claude-sonnet-5-5` through the Anthropic API (medium effort, tools set to `auto`, server-side fallback on) |
| **Inputs** | A free-text request (max 1,000 characters), the owner's pets, the planning date, and the tasks the owner already saved |
| **Outputs** | A schedule for one day and the week based on the date selected, a plain-English explanation, a confidence score, or a specific question for the owner. Nothing is saved until the owner clicks **Add to my plan**. |
| **Intended use** | Helping a pet owner organize routine care (walks, feeding, playtime, grooming, reminders) |
| **Not intended for** | Medical or veterinary decisions, medication dosing, or unattended use. It is a planning aid, not a source of care advice. |
| **Evidence** | 113 passing tests, a 10-case evaluation in offline and live mode ([`evaluations/results/`](evaluations/results/)), and step-by-step logs in `agent.log`. See the README's Testing sections. |

## How reliability is built in

- **Input guardrails:** Blank, oversized and pet-less requests and a missing API key are rejected before any API call.
- **Validated tools:** TRhe model can only act through four tools that check every pet name, time, duration, priority and date. Bad input becomes an error the model can read and fix.
- **Our own checker:** After each attempt, code (not the model) rebuilds the schedule and looks for conflicts and unscheduled tasks.
- **Self-review:** Claude rates the finished plan against the request. A rating below 0.7 sends it back for revision.
- **Attempt cap and questions:** At most 3 attempts, then a specific question for the owner instead of a guess.
- **Confidence score:** The average of a rule-based score and the model's rating, with a warning below 0.7.
- **Locked saved tasks:** The assistant can schedule around tasks the owner saved earlier but cannot remove or change them.
- **Logging and error handling:** Every request, tool call, check and failure goes to `agent.log`, and API failures become friendly messages.

## Limitations and biases

### Limitations I observed

- It is not deterministic. The same kind of request can get different handling. Asked to give Max joint supplements "at noon" with no duration, one run asked for the missing details and a later run quietly assumed 5 minutes, even though the instructions forbid inventing durations.
- The confidence score is a heuristic, not a probability. The model's self-rating was sometimes too generous (0.90 for a plan with an invented supplement duration) and sometimes too picky (it sent a sensible plan back twice over an ambiguous phrase). The rule half of the score only measures attempts and free time.
- It plans one day at a time. A task that repeats on other days (for example Mon/Wed/Fri) does not appear when the planned date is a Sunday. The app now says "saved, but not due", but it can be confusing.
- It cannot edit or remove saved tasks. It can only add new ones around them.
- Cost and speed: One request makes several API calls and took roughly 5 to 25 seconds in testing. It needs internet access and API credit.
- Generic error wording: Several API failures, including a 400 caused by an empty credit balance, show the same "The API returned an error (400). Try again shortly." message, and the log records only that short message.
- The offline evaluation cannot test the model. Its replies are scripted, so a 10-of-10 offline result only proves the surrounding code works. The live evaluation is one run per case unless `--runs` is used.

## Biases and assumptions built into the system

- **English and US conventions:** Prompts, examples and the way times are described ("8am", "5pm", "evening") assume English and a US-style day. Other languages, 24-hour phrasing or different daily rhythms were not tested.
- **Defaults chosen by the model:** When the owner says "evening" or "morning" without a time, the model picks one (it chose 18:00 for "evening"), and it chooses priorities itself (it treated medication as high and walks as medium). These reflect the model's idea of a typical household, not the owner's.
- **Examples skew toward dogs and cats:** Everything was tested with a dog and a cat. Pets with unusual routines (fish, reptiles, horses, multiple medications per day) were not tried.
- **Short, tidy requests:** Test requests were clear, short English sentences. Rambling, ambiguous or emotional requests were barely tested beyond "Do something nice for my pets".

## Could it be misused and how is that prevented?

| Risk | What could happen | What reduces it | What is still missing |
|:--|:--|:--|:--|
| **Trusting it for medication or health care** | A wrong or assumed time or dose is treated as veterinary guidance | The owner must click **Add to my plan**; low confidence shows a warning; the assistant is scoped to scheduling and gives no care advice | It can still assume a duration, and the confidence score does not flag every invented detail. A "this value was assumed" marker is needed for anything medical. |
| **Prompt injection** (a request that tries to change its instructions) | The model is told to ignore its rules | Its only abilities are four scheduling tools that validate their input; it has no file, network or code access, and the worst outcome is junk tasks in one session | The system prompt is not hardened against injection, and it does not refuse off-topic chat beyond asking for a pet care request. |
| **Running up the API bill** (if the app were public) | Repeated requests spend the owner's credit | 1,000-character limit, 3-attempt cap, 12 tool turns per attempt | There is no login, rate limit or spending cap, so the app must not be exposed publicly as is. |
| **Privacy** | Pet names, health conditions and medications are sent to the Anthropic API and also written to `agent.log` | `agent.log` and `.env` are gitignored and stay on the local machine | Logs are not rotated or redacted. Anyone deploying this should tell users what is sent and stored. |
| **Leaking the API key** | A committed key can be stolen and billed | The key lives only in `.env`, which is gitignored | A pre-commit secret scan would be safer. |

## What surprised me

1. **The first live run found a bug that 100 passing tests missed.** The live evaluation passed 9 of 10. When the agent asked its final clarifying question, my code re-sent the conversation without the tools list, and the API rejected it with a 400 because Claude's saved reasoning is tied to that list. My scripted fake could never have caught this. The fix was to send the same tools with `tool_choice` set to `none`, plus a regression test.
2. **Hand-testing exposed five problems that no automated test had.** I tried multi-task requests, then repeated requests, and the log showed the problems: Claude's own questions were thrown away and retried (so it ended up inventing a duration); a plan with only one of two tasks was called clean; a repeating task not due that day produced an "ok" empty plan with 0% confidence; the rating prompt showed only today's tasks, so it kept reporting "missing" tasks; and the assistant did not know about tasks I had already saved. Each is now fixed and covered by a test.
3. **My own prompt pushed the model into the behavior it forbade.** I told it never to invent durations, but when it replied with a question my loop treated that as a failure and told it "No tasks were added", so on the next attempt it guessed. A rule in the prompt was not enough; the surrounding code had to respect it.
4. **The model's self-review was better than I expected, and also inconsistent.** It caught a daily task saved as one-time and a vet visit shifted by five minutes, with a one-sentence reason each time. But the same reviewer let an assumed supplement duration through at 0.90.
5. **A 400 error does not mean a bug.** My first live run failed on every request, and the cause turned out to be zero credit on the account. The generic error message made that hard to see and shows why error text should carry the API's own explanation.

## Collaboration with AI

**How I used AI.** I used Claude Code throughout, and I set the working method: go header by header through the assignment, discuss ideas before any code, and have every piece of code explained line by line in chat. Claude proposed the agent design, helped with the code and tests, ran the checks and read the logs. I chose the AI feature (an agentic workflow), the reliability pieces (logging, error handling, confidence scoring and an eval script), the LLM provider, and the 3-attempt cap; I decided what to keep or cut; I tested the running app by hand; and I reported the behavior that led to the largest fixes. I also considered switching providers when my API credits ran out and decided to add funds instead.

**One suggestion I accepted.** After my hand-testing, Claude read `agent.log` and turned my vague "it struggles with multiple tasks" into five specific causes and proposed four fixes (let the model ask directly; act on the self-rating; handle tasks not due that day; make the agent aware of saved tasks). I accepted them, did the first three first and the fourth after, and re-checked each against the real API. It was the right call because the diagnosis came from evidence in the log, not from guessing, and each fix came with a regression test. And it helped the assistant get accurate results.

**One suggestion I changed, and one I scoped down.**

- *Changed:* Claude proposed a revise cap of 4 attempts. I set it to 3, the low end of the 3-5 range I had asked for. Later testing supported that: even at 3 attempts, one multi-task request took about 25 seconds.
- *Scoped down:* Claude's plan included both a command-line entry point and a Streamlit text box. I dropped the command-line part and kept only the Streamlit box, which kept the project smaller and let me finish and test the part users actually see. When I suggested treating the date picker as the date tasks *start*, Claude recommended only relabeling it and explaining it, because changing the scheduler to plan across many days would have meant rewriting core logic on deadline day; I agreed.

**Where the AI was wrong.** Claude introduced two bugs of its own: the clarifying-question call that dropped the tools list, and a prompt-plus-loop combination that made the model invent durations. Both were found only by running against the real API and reading the logs, not by the tests Claude had written. That is why I treat AI-written code and AI-written tests as a starting point that still needs live checks and my own review. I also had to ask Claude to keep its explanations tied to each line, because the code only helps me if I can explain it myself.

## Future improvements

- Show the API's own error message for failures such as low credit, and log it.
- Mark any value the model assumed (such as a duration) and show it to the owner, with a stricter rule for anything medical.
- Check the confidence score against human ratings of real plans, and report the live evaluation over several runs (`--runs 5`) to measure consistency.
- Let the assistant propose edits or removals of saved tasks, with the owner's approval.
- Add login, rate limiting, a spending cap and log redaction before any public deployment.
- Test other languages, time formats and more kinds of pets.
