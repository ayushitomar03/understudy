# Understudy — design write-up

An LLM works out a task on a legacy banking screen once. What it did becomes a
typed capability an AI agent can call. After that the task runs with no model at all.

That is the brief's own through-line, so the interesting question is not whether
it can be built but **how much of a real workload it actually covers, and what it
saves.** This system was measured on 100 tasks across eight structural difficulty
tiers, and then on 74 more it had never seen.

| | tasks answered | model calls | cost |
|---|---|---|---|
| a model every time | 85 / 100 | 3,612 | $33.59 |
| the model, given the map | 87 / 100 | 3,104 | $29.28 |
| map + recipes, model as fallback | 84 / 100 | 2,799 | $25.84 |
| **no model at all** | **52 / 100** | **0** | **$0.00** |
| **no model, on 74 unseen tasks** | **57 / 74** | **0** | **$0.00** |

Those 52 free tasks cost about $12 of model time when a model did them. Surveying the
application, which is what makes them free, cost $9.72 once.

**Across all 174 tasks the free path never returned a wrong answer.** That is the
property the rest of this document is mostly about: a cheap automation that is
occasionally wrong about a balance is worth less than none, so everything the
system cannot be certain of, it refuses.

---

## 1. Architecture

Three layers, and one rule that orders them.

**The map** — one LLM pass over the application records its screens, the controls
on each one, the labelled values, the tables, which control leads where, and what
each of the application's messages *means*. Nothing enters the map that was not
resolved against the real page, because model prose about a screen has been
wrong in silent ways every time we relied on it: on this app the sign-on button
is an image whose only name is an attribute, the member field has no label tying
it to its text, and both live inside a frame a one-document reader never sees.

**The capability** — the first successful run of a task is written down as a
typed recipe (§2). It is filed under the *shape* of the goal, with argument
values masked out, so "member 40021's balance" and "member 40055's balance" are
one entry asked twice.

**The ladder** — `solve()` tries the cheapest thing first:

```
a recipe for this shape ──▶ replay ──▶ clean? ──▶ done, nothing spent
        │                      │
    none yet                 failed, recipe dropped
        └──────────┬───────────┘
                   ▼
   a plan built from the map ──▶ replay ──▶ clean? ──▶ done, nothing spent
                   │                          │
               refused                      failed
                   └────────────┬─────────────┘
                                ▼
                   the model, holding the map
                                │
                                ▼
                   recorded, and free from then on
```

**The rule that makes this safe: each rung may refuse, but none may lie.** The
model sits underneath as the floor, so the success rate is whatever the model
achieves and the free rungs only decide how much of it was free. A rung that
declines costs exactly what not having it costs. That is why the cost column
falls by 23% while the correctness column does not move.

### Key decisions and their trade-offs

**Accessibility tree, not DOM selectors.** The brief says to bias toward what
still works when there is no clean DOM. Locators are expressed as role+name,
"the control following this text", ordinal, or CSS as a last resort — and the map
records which strategy resolved. On this application the *primary* strategy is
"the control following text X", because the inputs carry no accessible name.
The cost is that anchors are visible text, which is the thing most likely to
differ between two tenants running the same product; §4 is how that is handled.

**Two things amortise, not one.** A recipe amortises a *task*; the map amortises
the *application*. The second turned out to be the stronger claim: of the 57
unseen tasks answered for free, **45 came from the map and only 12 from
recipes**. A system with recipes alone can only repeat itself. A system with a
map can do work nobody demonstrated.

**A symbolic planner, deliberately, on the free path.** Building a capability
from the map without a model means a small explicit grammar: which screen holds
the answer, the shortest route to it through the screen graph, which argument
goes in which field. It refuses whenever that is ambiguous — one unfillable field
and it declines rather than guessing which value belongs in which box. This is
the part a reviewer should push on, and the honest defence is that it is
measured: it answers 45 unseen tasks and is wrong on none of them, because every
way it could be wrong is a refusal instead.

**What was cut.** Earlier iterations built a repair loop, a judge, a proposer,
an orchestrator and a live dashboard — about 1,800 lines. They worked, and none
of them is in this repository: what is here is the path the numbers describe.
§7.

---

## 2. Artifact schema

A capability is a contract, not a step list — an agent has to know what it needs,
what it returns, and whether it is safe to call.

```python
Capability:
  id, version, goal, app{product, base_url}
  params  : [Param{name, type, example, required}]
  steps   : [Step{index, intent, action, target: Locator, value: Value,
                  risk, requires_human, postcondition, timeout_ms}]
  outputs : [Output{name, type, locator: Locator, at_step, pattern}]
  success : Check
  recoveries : [Recovery{code, detect, action, target, seconds, why}]
  approval, planned_from_map
```

**Why it is shaped this way.**

*Locators are a typed union with a reason attached.* Every `Locator` carries
`why` it was chosen and `verified`, whether it resolved against the real page. A
reviewer can read why a control is found the way it is; a maintainer can find
every unverified locator in a capability.

*Values reference secrets, never contain them.* A step that types a password
carries `from_secret: "pwd"`. The password is supplied at replay and never enters
the artifact or the log.

*Outputs are typed and can carry a predicate.* `pattern` is the cheapest thing a
caller can supply that generalises — not the answer, which differs per
invocation, but the shape of one. A balance is money-shaped for every member.
It turns "a value came back" into "the right kind of value came back" without an
oracle.

*Steps carry risk, not just action.* `risk: irreversible` is what the policy gate
reads to refuse an unattended transfer and escalate instead (§6).

*Recoveries live on the capability, not in the code.* An interruption is a
property of the application, so recipes inherit the ones the survey classified
rather than each learning about the same notice separately.

### The finding this schema exists to support

**A read anchored to a label survives; a read anchored to the data lies.** Both
look identical in the artifact:

```
after_text "Savings Bal."                         ← a field label. Survives.
role=row   "Date Type Amount 11/09 DEPOSIT 128.00" ← member 40055's only item.
after_text "Amount"                                ← a column header: row one, whichever row that is.
```

The second and third replay cleanly for the member they were recorded on and
silently return the wrong thing for anyone else. In one measured run, three
recipes were dropped mid-replay for this reason and two returned confident wrong
answers — an "oldest posted item" answered with the newest.

So a recipe is only stored if `cache.fit` can tell it will generalise: the read
must be anchored to something the survey recorded as *naming a value*, and where
a column header borrows a real label's name — `Amount` is both the transfer
form's textbox and the posted-items column — the control's role settles it.
Where the map has a labelled read for that output, it is swapped in and the
recipe is kept; where it has nothing, the recipe is declined, which costs exactly
what not caching costs. Tables are read structurally instead (§3).

---

## 3. Determinism & error handling

Replay walks the steps with no model: resolve the locator, act, check the step
landed, and assert the success condition at the end. Determinism comes from four
places — locators that were verified when recorded, postconditions per step,
an arrival check naming text that appears on that screen and no other, and
waiting for the page to settle rather than for a fixed interval. That last one
mattered: the member record returned 110 or 180 tree lines depending on render
timing, which silently turned every action into an apparent success.

**The result contract is the brief's three categories, and they are what every
message the survey meets gets classified into:**

| the application says | classified as | replay does |
|---|---|---|
| `NO MEMBER ON FILE`, `NO ITEMS POSTED IN PERIOD.` | **answer** | reports it as the result. Not a failure. |
| `MEMBER NO. MUST BE NUMERIC`, `AMOUNT NOT VALID` | **caller error** | stops and says the input was wrong |
| a maintenance notice standing in front of a screen | **interruption** | dismisses it by the control the survey recorded |
| `RECORD IN USE BY ANOTHER TERMINAL` | **busy** | waits the recorded interval and retries — there is nothing to press |
| `NOT AUTHORISED — this record is restricted` | **permission** | stops. Never retried; retrying locks accounts. |
| `SESSION EXPIRED` | **session** | re-authenticates, and refuses to replay a mutating flow from the top |

Classifying per application rather than per failure is deliberate: asked the same
question twice about identical screens, a model called `'ABC'` a business outcome
and `'40-021'` an unexpected screen. The map decides once, and every capability
inherits the same answer.

**What this map actually carries, as opposed to what the taxonomy supports.** The
survey ran against a quiet instance and met eleven messages, of which exactly one
is actionable — session expiry. It never met the held record, the maintenance
notice or the step-up prompt, so a planned capability here inherits how to sign
on again and nothing else. The machinery for the other rows is built and
exercised by tests; the map has no instance of them to carry. That is the single
biggest gap in this submission and it is the survey's, not replay's — see §7.

A failure returns what a person needs to debug it: the outcome class, the
subclass (`control_not_found`, `success_condition_unmet`, `policy_refused`), the
step it happened on, what was expected, what was observed, and a screenshot.
`evidence/demo/replay-error-state/` is one of these.

**Answers that are not fields.** Two kinds of answer have no labelled value to
read, and both were silently wrong before they were handled:

- *Rows.* A position is structural — "the third posted item" is the third data
  row whether the member has three or none — so `InTable(column, row)` is safe
  and is used. An *order* is not: the map records where a table is and what its
  columns are called, not how it is sorted, so "the oldest item" is **refused**.
  That refusal exists because the alternative answered `14/09` when the answer
  was `02/09`.
- *Counts.* Where the application states its own count — `Items shown: 3 of 3`,
  `Active orders: 0` — that number is read instead of counting rows. It is right
  when the table is truncated by paging, and `Active orders: 0` answers "how many
  are active" where counting one cancelled row does not. And when the goal counts
  something the application does not separate — "how many INQUIRY entries"
  against a line reading `Entries shown: 4` — the plan **refuses**, because
  answering it returned 4 where the answer was 2.

**On UI drift**, which the brief rightly calls the secondary concern: the map
records `verified` per locator and a static self-check refuses to save a map
that cannot be planned from. Drift shows up as `control_not_found` naming the
locator that stopped resolving, rather than as a wrong answer.

---

## 4. Heterogeneity & multi-tenant

**Surfaces.** The `Surface` protocol is `observe / navigate / click / type /
select / read / resolve`, and the browser is one implementation of it. Everything
above it — the map, the planner, replay, the policy gate — is written against the
accessibility tree, which is what a screen reader sees and roughly what an
OS-level automation layer exposes. Porting to a desktop surface means a new
`Surface`, not a new replayer. The locator union is already surface-neutral in
three of its four members; `Css` is the honest admission that a capability using
it will not port unchanged.

**Tenants running the same product.** A capability is bound to an *application*,
not to an address. This is not a claim, it is a bug that was caught by
measurement: recipes were first keyed by URL, so a recipe recorded against one
instance was refused by the policy gate on another — every cross-instance replay
blocked at its first step. The no-model score sat at 23 until it was fixed and
was 43 immediately after. Keying on the product identity and retargeting the base URL at
replay fixed it — the same change that makes one recipe usable by a hundred
tenants running that vendor product.

Beyond the address, tenants differ in branding and version, which is where the
text-anchored locators are weakest. The design intent is an overlay per tenant:
the shared capability plus a small diff of the anchors that differ, with the
shared one degrading to a refusal rather than a wrong answer when an anchor stops
resolving. **A second tenant was not stood up, so this is designed and not
demonstrated** — the honest gap in this section.

---

## 5. Escalation & handoff

**Detecting stuck.** Three conditions raise an intervention: discovery hits a
stopping condition (out of model responses, past its time limit, four
attempts in a row that changed nothing, or bouncing between the same screens),
replay meets something it cannot recover from, or a step is classified
irreversible and no person has authorised it. The third is the common one and
it is a *policy* decision rather than a failure — the system is working
correctly when it refuses to post a transfer unattended.

**Routing with enough to act on.** The request carries the capability and goal,
the step it stopped on, the live URL, a screenshot, an operator address, and why
it stopped in plain words. From the run in
`evidence/demo/escalation-asked-for-a-human/`:

> *"A human needs to explicitly authorize (opt in to) recording/replaying the
> irreversible 'Post Transfer' confirmation click for this funds-transfer flow,
> or tell me the correct mechanism/parameter to record that explicit opt-in so
> the flow can be finished successfully."*

**Taking the live session.** The brief is specific that the human operates the
same session, so nothing is torn down: the browser stays open on the page where
the run stopped, with its cookies and half-filled form, and what changes hands is
*permission to touch it*. That permission is a lease with exactly one holder —
`agent`, `human`, or `none` — asserted by every automated action before it runs,
which turns "who is in control" from a convention into something checked. Two
actors driving one session is the failure this prevents, and it is not
hypothetical: the moment a person clicks while automation is mid-step, the run's
record of what happened stops being true.

The lease lives in `control.json` rather than in memory, because the operator is
a different process — which also makes the handoff inspectable afterwards. While
a human holds it, a recorder captures what they actually did, so the manual steps
can be folded into the capability instead of being lost.

**Handing back** is releasing the lease; the runner observes where the human left
the session and carries on from there rather than restarting.

**The gap, stated plainly.** The evidence run shows the whole shape of it —
`control_released` with the context above, then `control_taken` by `human`, then
`control_returned` 45 seconds later — but those 45 seconds are the escalation
timeout expiring, not a person acting. Detection, routing and the lease are real
and fire in real runs; **a person taking the session, doing the step and handing
back to a run that then continues has not been demonstrated.** It is the first
thing I would finish.

---

## 6. Safety

**An allowlist that is checked, not documented.** A policy names permitted
origins and routes and the action types allowed, and every action asserts it
before running. A capability recorded against one instance and replayed at
another is refused by origin — which is how the tenant-keying bug in §4 announced
itself, rather than by quietly automating the wrong system.

**Irreversible actions are a class, not a judgement call.** Steps are classified
at record time. In `unattended` mode an irreversible step is refused and
escalates; in `attended` mode it may run because a person is present and
accountable. The opt-in is explicit, scoped to the flows that move money, and
recorded in the result. Note what it does *not* change: the action is still
classified irreversible and still logged as such. The opt-in changes who may
authorise it, not what it is.

**Secrets never become data.** Credentials are referenced by name in artifacts
(`from_secret: "pwd"`) and supplied at invocation. Session tokens are scrubbed
from observations before anything is hashed or stored, which was found the hard
way: unscrubbed tokens made every page hash unique and defeated the change
detection.

**The limits, honestly.** The allowlist is origin- and route-level, not
field-level: nothing stops a permitted flow from typing a wrong-but-permitted
value. Redaction is pattern-based, so a novel PII format in an unexpected place
could reach a log. Attended mode trusts the caller's assertion that a person is
present. And the escalation timeout means an unanswered request eventually
abandons the run — safe for a read, but a mutating flow that stops half-way needs
a compensating action the system does not have.

---

## 7. Cuts, and what I would build next

**Cut, and why.**

- *A self-improving loop* — a judge scoring failures, a proposer diagnosing
  them, a repair cycle rewriting capabilities and measuring whether the change
  helped, an orchestrator running it until a capability replayed perfectly, and
  a live dashboard. About 1,800 lines, and it worked — the wait on a held record
  was learned by that loop when the survey never met one. It is not in this
  repository, and its absence was measured rather than assumed: the no-model arm
  scores 52/100 without the knowledge it left behind and 51/100 with it, a
  difference inside the noise the probabilistic hazards produce anyway. The brief's problem is not "improve the
  flow", it is "do the work reliably and cheaply", and the loop made the system
  harder to reason about without moving that number — so it was cut rather than
  left lying around for a reader to wonder about.
- *A second tenant* (§4), and *desktop* — the seam is designed and neither is
  built.
- *Screenshot-and-coordinates targeting* — the accessibility tree carried this
  application, and adding a second perception mode before the first was exhausted
  would have been breadth instead of depth.

**What I would build next, in the order the numbers justify it.**

1. **A survey that knows when it is finished.** The same command surveyed this
   application as 16 screens one run and 3 the next, because the model decides
   when it has seen enough. Everything downstream is priced off that one pass —
   and 12 of the 17 held-out failures are the map simply missing a screen or a
   field. Coverage-based stopping: do not finish while a mapped control leads
   somewhere unvisited.
2. **Finish handback** (§5) and demonstrate the round trip.
3. **An answer that is an absence.** "NO ITEMS POSTED IN PERIOD." means zero to a
   person; the system reads no count line and fails rather than inferring. Two
   held-out tasks turn on exactly this.
4. **Verify a recipe by trying it, instead of inspecting it.** `cache.fit`
   decides generalisation by looking at the locator's shape. Replaying a fresh
   recipe once against a second argument — free, no model — would catch the same
   faults by observation, and ones I have not thought of.

**What I would want a reviewer to take from this.** The system does not get its
cheapness by being confident. It gets it by knowing what it does not know: of
174 tasks run with no model, it answered 108 and refused the rest, and it was
never wrong. Every refusal in this codebase has a specific wrong answer behind
it that the system produced first, and each one is pinned by a test.
