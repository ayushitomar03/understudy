# Understudy

Automation for back-office banking software that has no API — the kind you can
only drive through the screens, the way an operator does.

**The problem.** Banks run old back-office systems with no API. The only way to
automate them is to click through the screens. You can have an AI model do that,
but it is slow (about 2 minutes a task), it costs money every time, and it can
make a different mistake each time it runs.

**The idea.** The AI figures a task out, and plain code repeats it. Working out
a new task needs judgement, so the AI does that. Doing a known task again doesn't,
so a saved recipe does it: fast, free, and the same way every time. The AI stays
on call for new tasks and for anything the recipe isn't sure about.

1. **Map the app once.** The AI walks through the whole application one time and
   writes down a **map**: every screen, button, field and table, and what each
   error message means (for example, "record in use" means wait and try again).
2. **Learn a task once.** The first time someone asks for a task, the AI does it
   on the real screens. The steps that worked are saved as a **capability**: a
   plain recipe with inputs (like a member number) and outputs (like a balance).
3. **Replay without the AI.** After that, the task runs straight from the recipe
   in a real browser with no AI involved. It takes about 7 seconds and costs
   nothing. The map also lets the system build recipes for tasks nobody has shown
   it yet.
4. **Stop when unsure.** If the replay hits something it does not recognise, it
   stops and hands the task back to the AI or a person instead of guessing.

**How it did.** Tested on 100 tasks at eight difficulty levels, plus 74 tasks the
system had never seen. Every answer is checked by code against the data the app
actually holds, with no AI grading:

| | tasks answered | model calls | cost | per task |
|---|---|---|---|---|
| AI every time | 85 / 100 | 3,612 | $33.59 | ~2 min |
| **no AI**, recipes and map | **52 / 100** | **0** | **$0.00** | 7 sec |
| **no AI**, 74 unseen tasks | **57 / 74** | **0** | **$0.00** | 7 sec |

"Per task" means each task, run on its own in a real (headless) Chrome browser
against the live app. The 74 unseen tasks take about 6 minutes run one after
another, or about 90 seconds with 4 running side by side.

Those 52 tasks cost about $12 when the AI did them. Building the map that makes
them free cost $9.72, once.

**Across all 174 tasks, the no-AI path never gave a wrong answer.** The tasks it
missed, it stopped on and handed back. It did not guess. That matters more to a
bank than the cost saving.

## Why this is better than running an AI agent every time

| | AI agent every time | **Understudy** |
|---|---|---|
| Who works out the steps | the AI, every run | the AI, once per task |
| Speed | ~2 min a task | ~7 sec a task |
| Cost per run | model calls every time | none after the first run |
| Same steps every run | no, it can choose differently | yes |
| New task nobody set up | yes | often, from the map (45 of 57 unseen tasks answered) |
| Knows what an error message means | re-guesses each time | decided once per app, reused by every task |
| When unsure | may guess | stops and hands over |

Understudy uses the AI for what it is good at (working out something new) and
plain code for the rest (doing the same thing again, fast and the same way).

## Why it works

1. **These apps change slowly.** Bank back-office screens stay the same for
   years. Steps that worked once keep working, so recording once and replaying
   many times is safe here, even though it would not be on a website that
   changes every week.
2. **Controls are found the way a person finds them.** Legacy apps have no
   stable IDs. A recipe finds each field by what is on screen, such as "the box
   after the text *Member No*", and every locator was tested on the real page
   when it was saved.
3. **Every step is checked.** After each click, replay confirms it reached the
   screen it expected. At the end it checks the success condition before
   returning an answer. A step that did nothing cannot quietly pass as success.
4. **Error messages are understood once, not guessed every time.** The map
   records what each message means: *NO MEMBER ON FILE* is a real answer,
   *MUST BE NUMERIC* means the input was wrong, *RECORD IN USE* means wait and
   retry, *NOT AUTHORISED* means stop. An AI asked about the same screen twice
   gave two different answers, so the map decides once and every task uses it.
5. **Every layer is allowed to say "not sure", and none is allowed to guess.**
   Understudy tries the free options first: a saved recipe, then a plan built
   from the map. If either one is unsure, it passes the task to the AI, and the
   AI can pass it to a person. The worst case costs the same as using the AI
   every time. The best case costs nothing. That is why the no-AI path answered
   109 of 174 tasks and got none wrong.

**Limits, stated plainly.** The survey that built the map ran on a quiet copy of
the app. Of the runtime problems it can handle, the only one it actually saw was
session expiry. The code for the others (record in use, maintenance notice,
supervisor approval) is built and covered by tests, but the map has no example
of them yet. See [REPORT.md §3 and §7](REPORT.md).

The design write-up is in [REPORT.md](REPORT.md).

---

## Setting up

Python 3.11+, and Docker is not needed — the target application runs locally.

```bash
git clone <this repo> && cd understudy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

**Credentials.** Anthropic API access is needed only for discovery and for
surveying an application — never for replay. The Claude Agent SDK picks up
`ANTHROPIC_API_KEY` from the environment, or an existing Claude Code login.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # not needed for the replay demo below
```

**The target application.** MERIDIAN CORE 4.2 is a small back-office banking
system written for this project: frameset layout, tables nested four deep, inputs
with no accessible name, image buttons, and switchable hazards — a record held by
another terminal, an interstitial notice, a session that expires, a step-up
authorisation, a slow screen. It stands in for the class of application the
system is built for.

```bash
.venv/bin/python targets/meridian/app.py 8090
```

Sign-on is `tlr01` / `vault`. Members `40021`, `40055`, `40204` are open; `40113`
is restricted; anything else is not on file.

## Running it without live model access

Everything below runs against a local application with **no API key and no model
calls**, because replay is the production path and the production path has no
model in it.

**See what an agent can call, then call one** — the demo worth seeing first:

```bash
.venv/bin/python cli.py catalog
```


```bash
.venv/bin/python cli.py invoke meridian.members.read_balances --arg member_no=40055
```

**Run the whole no-model arm** over the hundred-task benchmark (~3 minutes):

```bash
PYTHONPATH=. .venv/bin/python tools/arms.py --set trained --arms D --workers 4
```

**…and over the held-out set** the recipes were never made from (~90 seconds):

```bash
PYTHONPATH=. .venv/bin/python tools/arms.py --set holdout --arms D --workers 4
```

**Confirm the held-out answers are real** before trusting the score — every
expected value is fetched from the screen the task has to reach:

```bash
PYTHONPATH=. .venv/bin/python tools/check_holdout.py
```

**Tests** (106, no model, needs the app running on 8090):

```bash
.venv/bin/python -m pytest tests -q
```

## The demo path: discover, then replay

This is the whole idea in two commands. The first one uses the AI; the second
does not.

```bash
# 1. An LLM works out a task it has never seen and records what it did.
PYTHONPATH=. .venv/bin/python cli.py discover \
    "Look up member {member_no} and report the savings balance" \
    --id demo.savings --base-url http://localhost:8090 \
    --param member_no=40021 --output savings \
    --credential uid=tlr01 --credential pwd=vault

# 2. Replay it with a different member. No model is involved.
.venv/bin/python cli.py replay capabilities/demo.savings.json --param member_no=40204 \
    --credential uid=tlr01 --credential pwd=vault
```

To survey an application from scratch — the one-off that makes tasks free later:

```bash
PYTHONPATH=. .venv/bin/python tools/map_app.py --app http://localhost:8090   # ~$10, 16 min
PYTHONPATH=. .venv/bin/python tools/map_tables.py                            # free, 30 sec
```

## Reproducing the benchmark

The benchmark runs the same 100 tasks four ways, to measure what each part of
the system adds. The code and results files call each way an "arm" (A–D).

| | What it is allowed to use | The question it answers | Result |
|---|---|---|---|
| **A** | the AI on every task, nothing else | How well does an AI agent do on its own? This is the baseline. | 85/100, $33.59 |
| **B** | the AI on every task, plus the map | Does the map help the AI? | 87/100, $29.28 |
| **C** | starts with nothing saved. Tries a saved recipe first, uses the AI if there isn't one, and saves every new success | Does learning as it goes cut the cost without losing accuracy? | 84/100, $25.84, 21 tasks answered with no AI |
| **D** | no AI at all. Only the recipes C saved, plus plans built from the map | What can the system do once it has seen the app, with no AI? | 52/100, $0, no wrong answers |

How to read it:

- **A vs B:** giving the AI the map makes it cheaper (3,104 model calls instead of
  3,612) and no less accurate.
- **A vs C:** learning as it goes costs 23% less for about the same accuracy.
- **D:** the no-AI path on its own. It answers half the tasks for free and stops
  on the rest instead of guessing. The unseen-task result in the table at the
  top (57/74) is D run on 74 tasks that no recipe was made from.

To run it yourself:

```bash
# All four setups on all 100 tasks (~$74, ~3 hours)
PYTHONPATH=. .venv/bin/python tools/arms.py --arms A,B,C,D --workers 4

# A smaller trial: 12 tasks, the three setups that use the AI (~$9, ~20 min)
PYTHONPATH=. .venv/bin/python tools/arms.py --pilot 12 --arms A,B,C
```

Results are written to `evidence/arms.json` (the 100 tasks) and
`evidence/arms-holdout.json` (the 74 unseen tasks).

## What is where

```
understudy/
  sitemap.py      the map: screens, controls, fields, tables, message meanings
  cache.py        recipes filed by task shape, and the rule for which are safe to keep
  plan.py         builds a capability from the map alone, with no model
  solve.py        the ladder: recipe, then map, then model
  artifact/       the capability schema — the contract an agent calls
  replay/         deterministic execution, recoveries, structured results
  discovery/      the AI learns a task, and the mapper surveys the app
  surface/        the browser surface: accessibility tree, locator strategies
  policy.py       allowlist, irreversible actions, what needs a person
  handoff.py      handing the live session to a human, and taking it back
  catalog.py      the door an AI agent calls through

tools/
  arms.py         the benchmark: four arms over a task set
  tasks100.py     100 tasks, eight difficulty tiers, answers from the app's fixtures
  tasks_holdout.py  74 tasks the system was never trained on, generated not chosen
  check_holdout.py  proves every expected answer is reachable before scoring
  map_app.py      survey an application into a map (uses a model)
  map_tables.py   add its tables to the map (no model)

evidence/
  demo/           a discovery, a replay from a recipe, a replay that hits an
                  error state, a replay refused with no model, and an escalation
  arms.json       the benchmark results quoted above
  sitemaps/       the map this system runs on
  cache/          the recipes it has recorded
```

`evidence/runs/` holds 2,178 raw runs and 378MB of screenshots; it is not
committed. The five runs worth reading are copied into `evidence/demo/`.
Earlier iterations' results and capabilities for targets this repository does
not ship are also left out — see REPORT.md §7.
