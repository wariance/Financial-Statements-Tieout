# Financial-Statements-Tieout

Financial Statements tieout and Disclosure notes review.

A local, deterministic first version for TB-to-FSLI mapping, cross-statement
checks and numeric notes checks. Python 3.9 or later; no external packages,
API keys or cloud uploads required to run the application.

## Start

From this repository folder:

```sh
python3 -m tieout serve
```

Open **http://127.0.0.1:8765**. Load the synthetic demo, inspect the mapping,
choose a tolerance and run the review. Use your own name and a configuration
reason for real engagements. The server listens only on this computer.
Stop it with Ctrl+C. Select another local port with `--port 8766`.

```sh
python3 -m unittest discover -s tests -v
```

## What this version does

- Many-to-one TB account mapping to financial statement line items, with
  explicit presentation sign and reviewer confirmation.
- TB balance and mapping completeness checks. Unmapped balances are measured
  gross, so offsetting omissions cannot hide one another.
- Versioned rule catalog in [rules/core.json](rules/core.json), with a readable
  [rule checklist](docs/rule-checklist.md).
- Automatic note-to-FSLI discovery using configurable topic and table-caption
  aliases in [rules/note_matchers.json](rules/note_matchers.json). Note numbers
  are evidence references, never semantic mappings.
- Unit normalization before comparison, using decimal arithmetic.
- Global absolute tolerance from **0 to 50,000**, inclusive, in the engagement's
  base currency. Per-rule overrides include an explicit zero. A threshold of
  50,000 accepts differences from −50,000 through +50,000.
- Separate `EXACT_MATCH`, `WITHIN_TOLERANCE`, `FAIL`, `NOT_TESTED`,
  `REVIEW_REQUIRED` and `NOT_APPLICABLE` outcomes.
- Evidence with original and normalized amounts, source file/cell/page
  references, formula, effective tolerance, settings and input/rule snapshots.

The default tolerance is zero. A nonzero difference within tolerance remains
an exception for review. Tolerance never excuses missing mapping or absent
confirmation. No extra rounding allowance is silently added. These settings
are numeric reconciliation thresholds, not audit materiality.

## Inputs and scope

Upload a structured engagement JSON following [examples/demo.json](examples/demo.json).
Amounts should be decimal strings, not binary floating-point numbers. Every
reported fact has a unique key, value, unit multiplier, source and location.
The engagement supplies one entity, period and currency; a `basis` identifies
the reporting scope. Blank or null amounts are not zero. Enter an explicit
zero only when supported by the source.

TB balances are in base currency units, debit positive and credit negative.
Mapping `sign` is 1 or −1, so a credit balance may become a positive reported
liability. Mapping requires a named reviewer and reason when confirmed.
Do not include subtotals as additional TB accounts. One account may map to
one FSLI; multiple accounts may map to the same FSLI.

This first version uses structured, human-checked inputs. It does not yet
extract PDF/Word/Excel files, propose AI mappings, split one account between
FSLIs, post adjusting entries, translate currencies or assess narrative
disclosure completeness. Provide an already adjusted TB when required and
identify it clearly in its source references. Nonmatching reporting scopes
require review. Conditional rules require an explicit applicability decision.

Current/prior comparisons require separately named facts (for example `PY.*`)
and verified source period labels. This schema does not independently verify
document periods or authenticate the original source files. The numeric
catalog is a starting checklist, not a complete US GAAP compliance review.

## Notes can have different numbers at every company

Use `notes` objects containing each note's identifier, heading, text and table
rows. Each row includes a caption, amount, unit, source location, period,
currency and reporting basis. A company's Note 3 and another company's Note 8
can both match PPE when their content supports that relationship. Changing
note numbers or document order does not change the arithmetic result.

Matching combines note topics with row captions, using configurable English
and Chinese aliases. It distinguishes net/gross and current/noncurrent
captions. A unique compatible candidate is compared automatically. Multiple
candidates or missing/incompatible scope information require review; no
candidate is `NOT_TESTED`. Missing values are never filled with zero.

The UI displays the selected source and alternatives. A reviewer can choose
a specific note row with a documented reason, then run again. Overrides
cannot bypass scope mismatches. Both the original input and matching
configuration are preserved with the run. This is deterministic alias-based
discovery over structured notes; raw PDF/Word parsing and broader language
understanding will be validated separately against representative drafts.

## Reproducible command line review

```sh
python3 -m tieout review examples/demo.json \
  --reviewer "Example reviewer" \
  --reason "Initial exact reconciliation" \
  --tolerance 0 \
  --output audit-runs/example-run.json
```

Use `--settings path/to/settings.json` for per-rule overrides and applicability
decisions. Its complete format is:

```json
{
  "tolerance": "0",
  "overrides": {"FS-01": "0"},
  "reviewer": "Reviewer name",
  "reason": "Documented engagement decision",
  "conditions": {},
  "not_applicable": {}
}
```

Rules are linear expressions of fact keys and numeric coefficients. They are
data; the application does not execute uploaded expressions. Changes to the
catalog require a new review run. The demo's optional `demo_settings` are
illustrative and are not implicitly applied by the command line.

## Audit records

Each completed browser run is saved under `audit-runs/`. Keep the complete
JSON result as the canonical record: it includes the input, rules, settings,
SHA-256 fingerprints and a UTC timestamp. Source references are supplied by
the preparer; fingerprints cover the structured input and rules, not original
documents. Reviewer names are self-declared. These records support tracing
and reproduction; they are not signed, independently authenticated or
tamper-proof. Keep original source documents with your engagement evidence.

Exports preserve all non-exact outcomes, including tolerance differences and
not-applicable decisions. Correlated differences must not be added together
as a total misstatement. Audit data and local inputs are excluded from Git by
default; keep client data out of source-control commits.

## Development

The calculation engine is independent of the browser interface. Catalog,
input and result snapshots make each run inspectable. The application is a
local prototype; hosted use would require identity, authorization, retention
and deployment controls.
