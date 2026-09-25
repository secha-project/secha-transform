# Findings: can a language model write the transformation code?

Runs of 2026-09-15 (phi4-14b, kimi-k3) and 2026-09-17 (codestral-2508), all against engine
`a0510a9`, rulebook `95de96b` and contract `325cc47c89a5`, on the same frozen cases. Each cell
holds 5 samples at temperature 0.2, with one repair attempt using synthetic feedback. The
method, and the corrections made to the harness during the runs, are in [README.md](README.md).

| Model | Served by | Kind |
|---|---|---|
| `phi4-14b` | TUNI Aviary | small open-weight model, hosted by the university |
| `moonshotai/kimi-k3` | NVIDIA NIM | large open-weight reasoning model |
| `codestral-2508` | Mistral API | commercial code model |

## Summary

- **Both large models wrote a correct transformer for one vendor every time.** kimi-k3 and
  codestral-2508 each got 10 of 10 programs exact on real data and edge cases, on the first
  attempt.
- **Every one of those programs broke when the configuration changed.** The 20 programs
  survived 0 of 60 drift cases. This is the cost the framework removes: the engine follows an
  edited rulebook with no new code.
- **Rewriting the engine separated the models.** On the vendor shown, kimi-k3 was exact 10 of
  10 times and codestral-2508 6 of 10. On the vendor not shown, they passed 4 of 10 and 3 of
  10.
- **kimi-k3's failures were mostly the contract's; codestral-2508's were its own.** Five of
  kimi-k3's six failures on the unseen vendor trace to two sentences of our contract that left
  a detail open. Every failing codestral-2508 program has a clear defect, most often that it
  cannot process the source shape it was not shown.
- **phi4-14b cannot do this task.** 1 of 20 programs passed on the vendor it was shown, none
  passed on another vendor, and repair did not help.
- **On a vendor none of them saw, the best generated engines followed the contract perfectly,
  and their output is still unusable.** Replayed on Kempower
  ([below](#kempower-the-same-programs-on-a-vendor-none-of-them-saw)), all 10 kimi-k3
  interpreters wrote exactly what the contract prescribes. The contract never anticipated that
  source, so its output gives every reading of a quantity the same identity: 5 identities for
  2,000 rows.
- **Mistral Medium did not run.** Mistral's free plan gives it a limit of zero requests.

## Scores

These are the strict scores from `python harness.py report --results results/phi4-14b
results/kimi-k3 results/codestral-2508`. A pass means every row and every field equals the
engine's output on every test input of the vendor shown: two real landing-zone inputs and one
edge case.

| Model | Mode | Rulebook shown | Pass, first attempt | Pass after repair | Row F1 | Stats exact | Identity hash | Pass on other vendor | Drift survival |
|---|---|---|---|---|---|---|---|---|---|
| kimi-k3 | snapshot | MX Electrix | 5 of 5 | 5 of 5 | 100% | 100% | 100% | not tested | 0% |
| kimi-k3 | snapshot | ProCem | 5 of 5 | 5 of 5 | 100% | 100% | 100% | not tested | 0% |
| kimi-k3 | interpreter | MX Electrix | 5 of 5 | 5 of 5 | 100% | 100% | 100% | 3 of 5 | 90% |
| kimi-k3 | interpreter | ProCem | 5 of 5 | 5 of 5 | 100% | 100% | 100% | 1 of 5 | 60% |
| codestral-2508 | snapshot | MX Electrix | 5 of 5 | 5 of 5 | 100% | 80% | 100% | not tested | 0% |
| codestral-2508 | snapshot | ProCem | 5 of 5 | 5 of 5 | 100% | 80% | 100% | not tested | 0% |
| codestral-2508 | interpreter | MX Electrix | 5 of 5 | 5 of 5 | 100% | 100% | 100% | 2 of 5 | 70% |
| codestral-2508 | interpreter | ProCem | 1 of 5 | 1 of 5 | 40% | 60% | 20% | 1 of 5 | 27% |
| phi4-14b | snapshot | MX Electrix | 0 of 5 | 0 of 5 | 0% | 0% | 0% | not tested | 0% |
| phi4-14b | snapshot | ProCem | 0 of 5 | 0 of 5 | 10% | 0% | 0% | not tested | 0% |
| phi4-14b | interpreter | MX Electrix | 0 of 5 | 0 of 5 | 34% | 0% | 0% | 0 of 5 | 10% |
| phi4-14b | interpreter | ProCem | 1 of 5 | 1 of 5 | 20% | 40% | 0% | 0 of 5 | 10% |

Row F1, stats and identity hash describe first attempts; the pass on the other vendor and drift
survival describe each program after repair. After repair, phi4-14b's row F1 rose to 19% and
14% in its two snapshot cells and to 35% for interpreter programs shown MX Electrix. No other
cell changed.

- kimi-k3 never needed its repair attempt. codestral-2508 used it on 6 of 20 samples, and
  none of those repairs changed a pass.
- In each snapshot cell, one codestral-2508 program emitted the right rows with wrong run
  statistics.
- The one phi4-14b program that passed wrote identity hashes that differ from the engine's, so
  its rows could not be merged into the same table. The codestral-2508 interpreter that passed
  on ProCem wrote the engine's identity hashes.
- The median generation time was 21 to 40 seconds for codestral-2508, 22 to 41 seconds for
  phi4-14b and 287 to 393 seconds for kimi-k3. kimi-k3's times include NVIDIA's queue: a probe
  waited 270 seconds before the first byte of a one-line reply. They say nothing about the
  model's speed.

## How failures are classified

This rule was written after three kimi-k3 failures had been diagnosed. Its scope was settled
after an automatic list of every failure's type had been produced, and before any failure was
classified or counted. codestral-2508 was run after the rule was written, and its failures were
classified under it unchanged.

The headline score is strict and is never adjusted. Separately, each sample that fails is
examined, so a reader can see which failures belong to the model and which trace to the wording
of the contract. A snapshot program failing a drift case is not examined: its configuration is
written into it, so it cannot follow an edited rulebook, and that cost is what drift survival
measures. Examining a sample stops at its first clear defect, because one defect settles that
the sample fails for a reason of its own.

A failure on one input is a **reading the contract permits** only when all three of these hold:

1. It traces to one identified sentence of [CONTRACT.md](CONTRACT.md) that is silent on the
   point or admits two readings.
2. The program follows one of those readings consistently, on every input where the point
   arises.
3. With that point set aside, the program's output on the input equals the engine's. For a
   program that crashes on the point, this is checked by changing only the code that handles
   it and running the program again. The changed program is never scored.

Anything else is a **defect**: a wrong value, a missing or extra row, a crash, a refused
import, a timeout, or a reading the contract rules out. A sample passes *except for permitted
readings* only when every one of its failures is a permitted reading. That count is reported
beside the strict one, never instead of it.

## What the classification found

**phi4-14b: every failing sample has a clear defect, so nothing changes.** Each of its 20
samples shows at least one of these:

- a crash on a name it never defined, such as `mapping` or `ts_utc`;
- a crash on a key that the other source shape does not have, such as `columns` or `src` on
  ProCem's long-shape rulebook and `rows` or `key_field` on MX Electrix's wide one, or on a key
  the contract makes optional, such as `shape` and `meter_field`
  ([CONTRACT.md:121](CONTRACT.md:121), [CONTRACT.md:127](CONTRACT.md:127));
- a crash on a missing key in its own written-in copy of the configuration;
- timestamps with a UTC offset converted or rewritten, where the contract says they stay
  unchanged ([CONTRACT.md:107](CONTRACT.md:107));
- only a fraction of the expected rows, or none.

**codestral-2508: every failing sample has a clear defect, so nothing changes.** All its
failures are interpreter programs, eight in all:

- six could not process the source shape they were not shown: five produced no rows for it,
  and one raised an error naming it, although the contract describes both shapes
  ([CONTRACT.md:127](CONTRACT.md:127));
- one assumed a meter field that ProCem does not declare ([CONTRACT.md:96](CONTRACT.md:96));
- four of the five shown ProCem also got ProCem itself wrong: epoch timestamps arriving as text
  left empty ([CONTRACT.md:24](CONTRACT.md:24)), the three millisecond digits dropped
  ([CONTRACT.md:110](CONTRACT.md:110)), or an unmapped record left uncounted
  ([CONTRACT.md:132](CONTRACT.md:132)).

**kimi-k3: five of its six failures on another vendor are permitted readings.**

| Rulebook shown | Pass on other vendor, strict | Except permitted readings | Drift survival, strict | Except permitted readings |
|---|---|---|---|---|
| MX Electrix | 3 of 5 | 4 of 5 | 90% | 90% |
| ProCem | 1 of 5 | 5 of 5 | 60% | 100% |

The two sentences behind them:

1. **An epoch written in scientific notation** ([CONTRACT.md:110](CONTRACT.md:110)). The
   ProCem edge case includes the epoch `1.781470800e12`, the same number as `1781470800000`.
   The contract says an `epoch_ms` value "is an integer number of milliseconds" and that "if it
   is not an integer, `ts_utc` is null". The engine applies this to the text: the string is not
   an integer, so the timestamp is null. One kimi-k3 program applied it to the number: the
   value is whole, so it wrote the timestamp, while a value with a fraction would still have
   been null. Both readings fit the sentence. The ProCem example in the prompt does show the
   engine's reading, but this program was shown MX Electrix's example, which has no epoch
   timestamps. Its output on the input was otherwise exact, statistics included.
2. **The shape of `phase_map`** ([CONTRACT.md:148](CONTRACT.md:148)). MX Electrix generates
   its harmonic columns from a `phase_map` written as a mapping from index to phase. The
   contract says only "for each `(index, phase)` pair of its `phase_map` in declared order",
   which does not say whether `phase_map` is a mapping or a list of pairs. ProCem's rulebook
   has no generated rules, so a program shown only ProCem had nothing to copy. Four of five
   kimi-k3 programs read it as a list of pairs and crashed on every MX Electrix input. With
   only that one read changed to accept a mapping, each was exact on all six MX Electrix
   inputs, drift cases and statistics included.

The remaining kimi-k3 failure is a defect. That program accepted an epoch only when it
arrived as a JSON integer, so it wrote an empty timestamp on every ProCem row. The contract
says delimited sources arrive as strings that the program must interpret
([CONTRACT.md:24](CONTRACT.md:24)).

The contract was not revised during the experiment, and the strict scores stand. A revised
contract for a later run would state both points.

## What this means for the thesis

- **Writing a transformer for one vendor is no longer the expensive part.** Two strong models,
  one open-weight and one commercial, did it correctly every time, on the first attempt. The
  expense is what follows: each change to the configuration made every one of those 20
  programs wrong. A generated transformer has to be generated and verified again after every
  edit, and verification needs an oracle, which here is the engine.
- **Writing the engine is much harder, and that is where the models differ.** Handling a
  vendor it has never seen needs a program that covers every rule the contract describes, not
  only the ones in its example. codestral-2508 mostly wrote programs for the shape it was
  shown; kimi-k3 covered both shapes and failed mainly where our contract left a detail open.
- **A generated engine still needs the real one.** Where the best interpreters disagreed with
  the engine, the cause was mostly a gap in the prose specification. The written contract did
  not pin the behaviour; the engine's code and its cases did. Generating the engine therefore
  does not remove the engine: checking the result needs one to test against.
- **Capability decides whether this works at all.** The 14B model available at the university
  was far from usable on this task.

## Limitations

- **One commercial model, and not the mapping benchmark's.** Mistral's free plan gives Mistral
  Medium, the commercial model of the mapping benchmark, a limit of zero requests, so the
  commercial arm is codestral-2508, Mistral's code model. The two steps do not share a
  commercial model.
- **Small samples.** Five samples per cell move a rate in steps of 20 points, and pass@k is
  estimated from those five.
- **Few inputs.** Each vendor has two real inputs, one edge case and three drift edits. A
  program can pass all of them and still be wrong on inputs they do not cover.
- **We wrote the contract.** It was written from the engine's documentation and frozen before
  any model output was seen, and two gaps were found in it. A different author would leave
  different gaps.
- **The classification rule came after some failures were seen.** Three kimi-k3 failures had
  been diagnosed before it was written, as stated above. It is applied to all three models
  alike, codestral-2508 was run after it was written, and it never changes the strict scores.
- **Different days.** codestral-2508 ran two days after the other two, against the same engine,
  rulebook, contract and cases, with the same prompts, sandbox and scoring. In between, the
  client began recording the model name each provider reports serving, and the report began
  counting missing and extra rows.
- **Hosted models are not reproducible by generation.** NVIDIA retired another model in this
  study on 2026-09-14, and Mistral's limit for Medium dropped to zero within two weeks of a
  successful probe. Cached replies re-score exactly; new replies from the same weights cannot
  be guaranteed. Mistral reported serving `codestral-2508` for all 26 of its replies.

## Kempower: the same programs on a vendor none of them saw

Replayed on 2026-09-25 under [kempower_replay/PROTOCOL.md](kempower_replay/PROTOCOL.md), which
was registered before the replay's code existed (commit `11d69cd`). The replay code is at
`74971f2`, and the protocol records two deviations, D1 and D2, neither of which changes a score.
No model was called. The 60 final programs of the September runs were run again, each verified
to be exactly what its cached model reply contained. All seven gates passed, among them a
positive control that is exact against the engine and a negative control that is caught, and the
replay ran once.

Kempower was landed after the programs were written, and the contract was frozen before that.
It is a wide source like MX Electrix, with three constructs the contract never describes: a
column's own aggregation, a row id taken from the row's position in its landed part, and
charging sessions. The engine absorbed them with five generic capabilities and no vendor logic
(onboarding diary, Step 4). The inputs were the golden contract's 6 synthetic records (24 rows)
and two samples of real landed parts chosen by a fixed rule: 200 records (1,000 rows) and 400
records (2,000 rows). The real samples hold no rejected record, no suspect value and no null
cell, so only the synthetic input exercises those paths.

### The contract's ceiling

The reference interpreter follows the contract exactly. On Kempower it ran on every input and
differed from the engine in three fields only: a null row id on every row, the default
aggregation on every state of charge and temperature row (9 of 24, 400 of 1,000, 800 of 2,000),
and therefore the identity hash. Every other field and every statistic matched the engine's.

For this source the identity hash is built from fields that are the same for every reading of a
quantity: one device, no clock time and, under the contract, no row id. So the reference writes
5 distinct `measurement_id` values per input: 5 for 24 rows, 5 for 1,000 and 5 for 2,000. The
canonical table merges on `measurement_id`, so a load that followed the contract exactly would
keep 5 Kempower rows, however many it read.

### Scores

A program is *contract-exact* when its output equals the reference interpreter's on all three
inputs: every row, every statistic and every identity hash.

| Model | Interpreters that run on every input | Pass against the engine | Contract-exact | Correct on everything the contract describes |
|---|---|---|---|---|
| kimi-k3 | 10 of 10 | 0 | 10 of 10 | 10 of 10 |
| codestral-2508 | 8 of 10 | 0 | 2 of 10 | 5 of 10 |
| phi4-14b | 0 of 10 | 0 | 0 of 10 | 0 of 10 |

Of the 30 snapshot programs, 29 run and write no row at all, and one crashes on a meter field
that Kempower does not have. They carry MX Electrix's or ProCem's configuration, written in.

All five predictions registered before the replay held, judged by code written before the run:
nothing passes against the engine on any input (P1); the reference departs from the engine in
exactly the three fields above (P2) and writes at most 5 identities per input (P3); 10, 2 and 0
interpreters are contract-exact, inside the registered bounds of at least 7, 2 to 5 and at most
1, with programs shown MX Electrix doing at least as well as those shown ProCem (P4); and no
snapshot program writes a row of the reference's output (P5). Two things qualify this. The
positive control is the reference with two patches, and it was exact before the replay ran, so
it had already implied most of P2 and P3; the protocol required that order. And kimi-k3's
bound of 7 was conservative.

### How failures are classified

The rule of the September classification applies, with the protocol's two additions, fixed
before the replay: criterion 3 is checked against the reference interpreter rather than the
engine, and a departure from the engine in a field the contract does not govern for this source
(the row id, the identity hash, the aggregation of state of charge and temperature, and any
session field) belongs to the contract, not to the model.

- **kimi-k3: nothing to classify.** All 10 interpreters wrote exactly what the contract
  prescribes, identity hashes included. In September, 6 of its 10 failed strictly on the vendor
  they were not shown, 5 of them on two points the contract left open. Kempower touches neither:
  it has no epoch timestamps and no generated rules.
- **codestral-2508: 5 correct on everything the contract describes, 5 with a defect.** Besides
  the 2 contract-exact programs, 3 programs shown MX Electrix followed the aggregation each
  column declares, a rulebook key the contract does not describe. With that set aside, each
  equals the reference exactly, statistics included, and its identity hashes are computed as
  Section 6 says. Its departures from the engine are the row id and the identity hash only, so
  it is closer to the engine than the contract is. The scorer's near-miss diagnosis also named
  `quantity` on 12 rows of these programs. That is its heuristic pairing a state of charge
  reading with a temperature reading of equal value, which it can do only because the row id is
  null; the comparison with aggregation set aside shows every quantity correct. The five
  defects:
  - one builds the device id from a meter field that Kempower does not declare (Sections 4.2 and
    4.4 make it conditional);
  - two shown ProCem leave the wide shape unimplemented, with a comment where it would go, and
    write no rows;
  - one handles only the long shape and rejects every record on every input, 606 in all, where
    the engine rejects 1;
  - one refuses the wide shape outright.

  Four of the five were shown ProCem. As in September, most codestral-2508 interpreters handle
  only the source shape they were shown.
- **phi4-14b: every program has a defect.** All 10 crash on every input, each on a rulebook key:
  five read a `datetime_format` and one a `timestamp_field`, keys for which the contract gives a
  default; three read keys of the long shape (`key_field`, `rows`) from a wide rulebook; and one
  reads a quantity from a record rule.

No permitted reading was needed. None of the four points the protocol declared silent caused a
failure: no program crashed on a record without a timestamp field or on the absent generated
rules. The phi4-14b program that crashed on `timestamp_field` read the rulebook key, which the
contract defaults, not the record's field.

### What no program did

- **No program wrote a row id,** although the rulebook declares `row_id_from: payload_position`
  and every record carries its position. So each of the 15 interpreters that wrote rows collapses
  identity to at most 5 values per input.
- **No program wrote a session,** because the contract's output has no session fields. The
  engine writes 2, 2 and 400 sessions on the three inputs.

### What this means for the thesis

- **The best generated engine followed its specification perfectly on an unseen vendor, and the
  result is still unusable.** kimi-k3's 10 interpreters wrote what the contract prescribes. Loaded
  into the canonical table, that output would keep 5 rows of any Kempower load, and it would
  record state of charge and temperature as averages. The failure is the specification's:
  written for two vendors, it could not say what a third would need.
- **The metadata-driven engine absorbs a new source shape; a generated one inherits the limits of
  its specification.** For Kempower the engine gained five generic capabilities, and the rulebook
  declared the row id and the aggregations. A generated interpreter would need its specification
  revised and the program generated and verified again, and that verification needs the real
  engine as its oracle.
- **Going beyond the specification happens, but only in part.** Three codestral-2508 programs
  followed a rulebook key the contract never mentions and got the aggregation right. None of them
  used the row id, which the same rulebook declares, so all three still collapse identity.
- **Per-vendor generated code gives a new vendor nothing.** 29 of the 30 snapshot programs ran and
  wrote no rows.
- **Capability still decides whether this works at all.** phi4-14b's interpreters crashed on
  every Kempower input.

### Limitations of the replay

- **The ceiling is our contract.** A contract written by someone else, or revised after
  September, would leave different gaps; the replay measures the contract as it was frozen.
- **One vendor and few inputs.** Kempower is one source, and its three inputs are small. The real
  samples exercise the normal path only.
- **Two predictions were settled early.** The positive control implied most of P2 and P3 before
  the replay ran, as the protocol's order required.
- **Small samples.** Ten interpreters per model, as in September.
