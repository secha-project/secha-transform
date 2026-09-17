# Can a language model write the transformation code?

An experiment, not part of the CI-gated contract. The framework's claim is that vendor
knowledge belongs in configuration, interpreted by one generic engine, rather than in a
hand-written transformer per vendor. A language model could appear to remove the cost that
argument rests on, by writing the per-vendor code for us. This experiment measures whether it
can, and what that code costs when the configuration changes.

## Why this is measurable here

The engine is a pure function: `transform_records(records, bundle, factors)` gives the same
rows for the same input. So any input has an exact expected output, and no answer key is
written by hand. A generated program receives precisely what the engine receives, and its
output is compared with the engine's row by row.

One rulebook object is used everywhere: it is shown to the model, passed to a program in
interpreter mode, edited for drift cases, and handed to the engine. A program and the oracle
can therefore never be working from different configurations.

The behaviour a program must reproduce is written down in [CONTRACT.md](CONTRACT.md), from
the engine's documented behaviour rather than its source, and frozen before any model output
was seen. Where the contract and the engine ever disagree, the engine wins.

## Two ways to ask

| Mode | What the model is asked for | Evaluated on |
|---|---|---|
| snapshot | a program for one vendor with its configuration written in, which is the hand-written transformer this framework replaced | that vendor's test inputs, and its drift cases |
| interpreter | a program that reads the rulebook at run time, which amounts to rewriting the engine | both vendors' test inputs, including the vendor it was never shown, and all drift cases |

## Cases

| Split | Contents | Where the data may go |
|---|---|---|
| dev | synthetic records with fabricated values: MX Electrix 3 records (41 rows), ProCem 12 triples | the only data ever placed in a prompt or in repair feedback |
| test | real landing-zone records (MX Electrix meters 21 and 22, 13 records each spread across the day; ProCem the first 200 lines of the day and 400 lines at even offsets) plus edge cases | local sandbox only, never sent to a model |
| drift | a test input scored against an edited rulebook | local sandbox only |

The drift edits are ordinary configuration changes, and each one is checked to change the
engine's output on its input, so a drift case can never pass by accident:

- MX Electrix: add the 9th voltage harmonic to the generated rule, remove the `pfl3` column,
  lower the frequency maximum from 65 to 50.
- ProCem: map the previously unmapped key 23541, remove the frequency entry, lower the
  voltage maximum from 1000 to 235.

`python harness.py build-cases` freezes every case with the engine's output, and records the
engine and rulebook commits and a hash of the contract in `cases/manifest.json`.

## Scoring

| Measure | Meaning |
|---|---|
| Runs | the program executed on every test input and wrote readable output |
| pass | the same multiset of rows as the engine, every field equal, values within 1e-9, nothing malformed, on every test input |
| Pass, timestamps as instants | as pass, but a timestamp written in another format for the same instant counts as equal |
| Stats exact | the run statistics equal the engine's |
| Identity hash | every `measurement_id` equals the engine's, so the output could be merged into the same table |
| Row F1 | row precision and recall summed over test inputs |
| Drift survival | the share of drift cases a program still passes |

Rows are matched as multisets on their identity, because row order is not part of the
contract and duplicates are. When a row is missing and another is extra, and the two share
device, row id, instant and value, the report names the identity field that separates them,
so "missing plus extra" becomes a diagnosis such as "wrong variant".

Each cell draws 5 samples at temperature 0.2, and pass@k uses the unbiased estimator of
Chen et al. (2021). A failing sample gets one repair attempt, with feedback computed only
from the synthetic development case, as a developer would see after running the program on
the example: the traceback, or counts and a few concrete mismatched rows.

## Data governance

The approval to use commercial endpoints covers partner metadata, not measurements. So:

- prompts and repair feedback are built only from synthetic development cases, and every
  function that builds them refuses a case marked as real data;
- a test asserts that no decimal of four or more digits from any real record appears in any
  prompt;
- `cases/` and `results/` hold real measurements and are gitignored; `.cache/` holds replies.

## Running generated code safely

This guards against accidental damage, not a hostile program. A static check refuses calls
such as `open`, `eval` and `exec`, and imports outside an allow-list. The allow-list refuses
only what the contract forbids: modules that reach files, the network, processes, the
environment or interpreter internals. Any other standard-library module is allowed, since the
contract permits it. The program
runs in a separate interpreter started with `-P -s -S`, so only the standard library can be
imported, in an empty directory, with an environment stripped of API keys and paths, and
with a timeout.

## Validating the harness before trusting it

A harness that scores programs has to be tested against programs whose correctness is known.
`reference/interpreter.py` implements the contract with the standard library, and
`python harness.py selfcheck` requires that:

- the reference is exact on all 14 cases, including every drift case, with exact statistics
  and identity hashes;
- the reference with one vendor's rulebook frozen into it passes that vendor's own cases and
  is caught by all 3 of its drift cases;
- two planted defects are caught: dropping the scaling factor, and dropping the `Z` from naive
  timestamps, which must fail strictly and pass on the lenient timestamp tier.

The unit tests (`pytest experiments/llm_codegen/tests`) cover the comparison, the metrics,
the sandbox, the client, the prompts, the report and the harness, and need no network.

## Platform findings that affect validity

Each was found before the model it affects was scored, and each would otherwise have been
scored as a model failure or have left an arm unrun.

- **TUNI Aviary truncates prompts intermittently.** Identical requests are alternately read in
  full and cut to exactly 4,096 tokens, which suggests replicas with different context windows.
  The client checks the provider's prompt token count on every reply, never caches a truncated
  one, and sends the request again; resends are recorded per generation.
- **`phi4-14b` has a 16k-token window.** Prompts dense with YAML and JSON run at about three
  characters per token, so a large example output would leave no room for the program. Large
  example outputs are shown as a labelled subset (the first row of each quantity per record and
  every suspect row, with statistics for the full output), and the program budget is 6,000
  tokens.
- **`deepseek-ai/deepseek-v4-pro-0813` was retired on 2026-09-14** and returns `HTTP 410 Gone`.
  The open-weight arm uses `moonshotai/kimi-k3`, which was verified to read a 12,800-token
  prompt in full. Reasoning models spend tokens before they answer, so it gets a 16,000-token
  budget.
- **Mistral's free plan gives its Medium and Small models no requests.** Mistral sets limits
  per model. On 2026-09-17 every Medium, Small, Magistral and Devstral model we tried answered
  HTTP 429 with `x-ratelimit-limit-req-minute: 0`, and Large answered 403 `tier_not_allowed`,
  while Codestral, Ministral and the embedding model answered on the same key. An earlier
  reading, that the whole workspace was blocked, came from trying only Medium and Small.
- **The commercial arm is `codestral-2508`.** It is Mistral's code model, served through its
  commercial API, and the task here is writing code. The dated name is requested, and the
  model name Mistral reports is recorded with every reply. It gets the same 16,000-token budget
  as kimi-k3, so the limit decides no result for either large model. `mistral-medium-2604`, the
  commercial model of the mapping benchmark, can be added once its limit is raised.

`python harness.py probe-context --model <model>` repeats a long recall probe without caching
or resending, so truncation is observed directly rather than worked around.

## Corrections made during the runs

Five harness defects surfaced while the first arms were running. Each was fixed with a
regression test and the self-check was run again. No reported number comes from a harness
that still had them.

- **The sandbox refused a module the contract allows.** One phi4-14b program imported `ast`.
  The contract permits the whole standard library, but the allow-list lacked `ast`, so the
  program was refused on every test input. Its repair was then told to obey a rule the model
  had never been shown.
- **Repair prompts differed on every run.** Feedback quotes the traceback, and a traceback
  names the program by its path in a randomly named temporary directory. So the same failure
  produced a new repair prompt each time. A repair could never be replayed from the cache, and
  a local path was sent to the model. Tracebacks now name the program `program.py`. Feedback
  for all 20 first-run phi4-14b programs was checked to be byte-identical across runs and hash
  seeds.
- **A provider's failure was recorded as the model's.** NVIDIA throttled one kimi-k3 request
  for longer than the client's one-minute retry window, and the sample was scored as a program
  that never arrived. A busy provider (HTTP 429, 502, 503 or 504) now has a wait budget of its
  own, 20 minutes, and `Retry-After` is honoured. A completion that still fails for the
  provider's reason is marked as a provider failure: busy past the budget, a server or network
  error, a stream that breaks off, a prompt that stays truncated, a missing key, or an empty
  reply that did not run out of tokens. The harness then records nothing for that sample and
  stops, so running the same command again retries it. The affected sample was removed, and no
  other recorded sample in any run carries a provider error.
- **Replies from NVIDIA were not streamed.** In a probe, NVIDIA held a one-line kimi-k3 request
  for 270 seconds before it began to answer, and its gateway returns HTTP 504 when no response
  has begun after about 300 seconds. A reply that is not streamed begins only once it is
  complete, so the queue and the whole generation had to fit in 300 seconds; the first
  programs took 263 to 287 seconds. A program that took longer to write would mostly have
  failed, which would have favoured short answers. Requests to NVIDIA are now streamed, so the
  response begins when generation does and only the queue counts against the limit. A streamed
  test reply of 14,000 tokens ran for 659 seconds without being cut. Streaming is left out of
  the cache key, so replies cached before the change are still used.
- **The report crashed when run statistics differed.** Recorded results were unaffected,
  because they are written before the report is built.

The phi4-14b arm was scored again on the corrected harness. Its first attempts are the cached
replies of the first run, so they are the same draws. A repair that follows a crash is a new
draw, because its prompt differs from the one the first run sent. Twelve repairs were drawn
again. One sample changed: the program that imports `ast` now runs and crashes instead of
being refused. No score in the report moved. The first run is kept in
`results/phi4-14b.superseded-ast-guard` for comparison. The kimi-k3 run was stopped and resumed
on the corrected harness. Its three remaining samples had passed on the first attempt, with no
repair and no provider error, so no correction could affect them.

## Running it

```bash
cp .env.template .env                        # add the provider keys
python harness.py build-cases                # freeze inputs and the engine's output
python harness.py selfcheck                  # the harness must pass before any model runs
python harness.py dry-run --mode snapshot --vendor mx_electrix
python harness.py probe-context --model phi4-14b --repeats 5

# one results directory per model, so separate runs never write the same file
python harness.py run --models phi4-14b --max-tokens 6000 --out results/phi4-14b
python harness.py run --models moonshotai/kimi-k3 --max-tokens 16000 --timeout 1800 \
    --out results/kimi-k3
python harness.py run --models codestral-2508 --max-tokens 16000 --delay 1 \
    --out results/codestral-2508

python harness.py report --results results/phi4-14b results/kimi-k3 results/codestral-2508 \
    --out results/comparison.md
```

A run writes its results after every sample and skips samples already recorded, so an
interrupted run resumes where it stopped. Replies are cached per provider, model, request and
sample index.

## Results

See [findings.md](findings.md).
