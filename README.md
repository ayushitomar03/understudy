# Understudy

Automation for back-office banking software that has no API — the kind you can
only drive through the screens, the way an operator does.

An LLM works a task out once. What it did is written down as a **capability**: a
typed, reviewable recipe an AI agent can call with arguments. After that the task
runs from the recipe with no model in the loop — seconds instead of minutes,
nothing instead of cents.

Before any of that, the LLM reads the whole application once and writes down a
**map**: the screens, the controls, the fields, the tables, and what each of the
application's messages means. The map is what lets the system do tasks nobody
ever demonstrated.

**Measured on 100 tasks across eight difficulty tiers, and on 74 more the system
had never seen:**

| | tasks answered | model calls | cost | per task |
|---|---|---|---|---|
| a model every time | 85 / 100 | 3,612 | $33.59 | ~2 min |
| **no model at all** — recipes and map | **52 / 100** | **0** | **$0.00** | 7 sec |
| **no model, on 74 unseen tasks** | **57 / 74** | **0** | **$0.00** | 7 sec |

Those 52 tasks cost about $12 of model time when a model did them. The map that
makes them free cost $9.72, once.

**Across all 174 tasks the free path never gave a wrong answer.** When it cannot
be certain of the answer it stops and hands the task to the model rather than
guessing — which is the only reason the cost saving is worth anything to a bank.

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

This is the loop the whole system exists for. The first command costs model time;
the second does not.

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

## Comparing the arms yourself

The four arms differ only in what the system is allowed to remember:

| arm | map | recipes | model |
|---|---|---|---|
| A | – | – | every task |
| B | yes | – | every task |
| C | yes | yes | only when the free path declines |
| D | yes | yes | **never** |

```bash
PYTHONPATH=. .venv/bin/python tools/arms.py --arms A,B,C,D --workers 4   # ~$74, ~3 hours
PYTHONPATH=. .venv/bin/python tools/arms.py --pilot 12 --arms A,B,C      # ~$9, 20 min
```

Results land in `evidence/arms.json` and `evidence/arms-holdout.json`.

## What is where

```
understudy/
  sitemap.py      the map: screens, controls, fields, tables, message meanings
  cache.py        recipes filed by task shape, and the rule for which are safe to keep
  plan.py         builds a capability from the map alone, with no model
  solve.py        the ladder: recipe, then map, then model
  artifact/       the capability schema — the contract an agent calls
  replay/         deterministic execution, recoveries, structured results
  loop/           the LLM discovery loop and the surveyor
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
