## Plan Assistant eval (live mode, 1 run(s) per case)

| # | Test | Expected | Actual | Attempts | Confidence | Result |
|:--|:--|:--|:--|:--|:--|:--|
| 1 | Simple task | ok | ok | 1 | 0.95 | Pass |
| 2 | Pinned time | ok | ok | 1 | 0.95 | Pass |
| 3 | Three tasks, two pets | ok | ok | 1 | 0.97 | Pass |
| 4 | Fixable conflict | ok | ok | 1 | 0.95 | Pass |
| 5 | Impossible conflict | needs_clarification | needs_clarification | 3 | - | Pass |
| 6 | Unknown pet | needs_clarification | needs_clarification | 1 | - | Pass |
| 7 | Vague request | needs_clarification | needs_clarification | 1 | - | Pass |
| 8 | Empty input | error | error | 0 | - | Pass |
| 9 | Oversized input | error | error | 0 | - | Pass |
| 10 | No pets added | error | error | 0 | - | Pass |

**10 of 10 runs passed.**
Average confidence on successful plans: 0.95; average attempts: 1.0.
