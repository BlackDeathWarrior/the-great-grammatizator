# docs/v2

Documentation rewritten to describe **what is actually built and running**,
after the platform was implemented and demonstrated against live providers.

Read in this order:

| File | What it answers |
|---|---|
| `POC.md` | Where the project stands, what broke, what to do next. **Start here.** |
| `ARCHITECTURE.md` | How it works. §12 records where v1's design and reality diverged; §13 is planned-not-built |
| `USE-CASES.md` | Operator-facing behaviour and the three demo scripts |
| `TEST-CASES.md` | The TC register reconciled against the suite, with a ranked gap list |

## Relationship to `docs/`

The originals in `docs/` are the record of **intended** design. They are not
deleted, for two reasons: code comments cite them by section number throughout,
and the gap between intent and outcome is itself useful information.

Where the two disagree, **v2 is what the code does**. `ARCHITECTURE.md` §12
lists the three places they diverge and why.

`docs/CLAUDE.md` is **not** superseded. It is the owner's working-style spec —
plan then act, define done, stay in scope, show evidence, ask before adding a
dependency — and still applies.

## Reading these as a future session

Three conventions matter:

1. **A section describing behaviour has a test id beside it.** No test id means
   the claim is unverified — treat it as a gap, not a fact.
2. **Anything under "planned, not built" is design intent with no code.** Do not
   treat it as an invariant.
3. **Measured numbers are marked as measured.** Latency and call-budget figures
   come from observed runs, not estimates.
