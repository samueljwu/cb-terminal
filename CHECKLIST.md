# Prospectus Extraction Checklist

This checklist governs all cb-terminal prospectus extraction, OCR, drafting, and review work.

## Extraction-layer contract

- PDF text-layer extraction is local and deterministic; the optional backend is PyMuPDF.
- Term extraction is deterministic parser/evidence matching, not generative extraction.
- OCR, if added, must follow the controls in this checklist before its output can support extracted terms.
- This checklist defines invariant extraction behavior. It must not depend on how many raw files, extracted contracts, pending reviews, or checked-in examples happen to exist in the workspace at any moment.
- If a PDF has no extractable text, the workflow must fail closed and report the gap; it must not infer terms from the filename, issuer name, prior examples, market knowledge, or model guesses.

## Core principles

1. No facts may be made up.
2. Every populated contract term must be traceable to source evidence from the raw PDF or an explicitly labeled page-text fixture.
3. Evidence must include page number, snippet, confidence/match type, source filename, and extraction method where available.
4. Evidence collection is not human approval. Draft contracts remain `status: needs_review` until reviewed manually.
5. Missing evidence means the field stays blank/`None`/`needs_review`; do not fill with defaults.
6. Do not use today’s date, assumed year-end dates, generic denominations, standard redemption prices, or common CB conventions as extracted facts.
7. If a document has multiple CB series, create one draft per explicit series and attach series-specific evidence.
8. Do not overwrite existing contracts during auto-ingest.
9. Do not delete or archive raw PDFs during extraction. Raw cleanup is a separate guarded reviewed-status + checksum flow.
10. If an LLM or OCR engine is ever added, it may only propose candidates; it must not be treated as authority without raw-PDF evidence snippets.

## Prohibited extraction behavior

Never populate a field solely from:

- Filename hints.
- Prior contracts.
- Market convention.
- “Usually” or “standard” CB terms.
- The current date.
- A maturity year without a day/month.
- An issuer/deal nickname without matching document text.
- An LLM-generated summary that is not tied to raw page evidence.
- OCR text that has not been reviewed for confidence/legibility.

Examples of prohibited defaults:

- `pricing_date = today`.
- `maturity_date = YYYY-12-31` from “due YYYY”.
- `denomination = 100000` unless the PDF says so.
- `issue_price = 100%` unless the PDF says so.
- `maturity_price = 100%` unless the PDF says so.
- `fixed_exchange_rate` inferred from market FX.
- `underlying_ticker` inferred from issuer identity rather than document text.

## Required source evidence fields

For each populated term below, attach `source_review.term_evidence[field]` with page/snippet evidence:

- `issuer.name`
- `bond.description`
- `bond.issue_size`
- `bond.denomination`
- `bond.issue_price`
- `bond.closing_date`
- `bond.maturity_date`
- `redemption.maturity_price`
- `conversion.underlying_ticker`
- `conversion.initial_conversion_price`
- `conversion.fixed_exchange_rate`
- `conversion.start_date`
- `conversion.end_date`
- `calls[0].trigger_ratio`

A field may be absent or blank if evidence is missing. That is preferable to a guessed value.

## Standard extraction procedure

1. Preserve the raw PDF.
   - Keep the source file under `data/raw/prospectuses/` or another approved raw-source location.
   - Compute and store SHA-256 before creating durable draft records.

2. Extract page text.
   - Use project venv for PDF-backed extraction:
     `.venv/bin/python`
   - Default text-layer backend: PyMuPDF.
   - Record extraction method, page count, extracted character count, warnings, and per-page text.

3. Fail closed if extraction is insufficient.
   - If no text is available, queue as `needs_extraction_backend` or equivalent.
   - If text exists but no conservative template matches, queue as `needs_manual_template`.
   - Do not create a contract from unsupported text.

4. Identify explicit series.
   - Detect each explicit convertible-bond series separately.
   - Require direct evidence for issue size and maturity date for each series.
   - For multi-series PDFs, emit one draft contract and one review-queue item per series.

5. Extract only evidence-backed scalar terms.
   - Populate a term only if the raw page text contains a matching snippet.
   - Attach the exact page/snippet evidence under `source_review.term_evidence`.
   - If evidence is ambiguous, leave the field blank/`needs_review` and add a review note.

6. Normalize units without changing facts.
   - FX convention must be standardized as stock currency per CB currency/output currency.
   - If the PDF states the inverse convention, store the converted value only with evidence showing the original quoted units.
   - Keep original evidence snippet visible for reviewer audit.

7. Generate a review report.
   - Include extraction method and warnings.
   - Include validation issues.
   - Include source evidence matrix.
   - Include evidence gaps.
   - Include manual review checklist.

8. Keep status conservative.
   - New auto-generated contracts must be `status: needs_review`.
   - `evidence_collected_needs_human_review` is not the same as `reviewed`.
   - Only a human-reviewed workflow may set `status: reviewed`.

9. Verify before mutating durable project state.
   - First run extraction into temporary output directories when adding parser coverage.
   - Confirm created/failed/needs-extraction counts.
   - Confirm each populated scalar term has page/snippet evidence.
   - Then run the full test suite before touching durable queue/contract outputs.

## If OCR is added later

OCR must follow these additional controls:

1. Store OCR output as page-level text with page numbers.
2. Preserve OCR engine name/version/settings in extraction metadata.
3. Store OCR confidence when available.
4. Flag low-confidence pages or terms as `needs_review`.
5. Do not use OCR output to silently overwrite text-layer extraction; record the method and warnings.
6. For tables or densely formatted termsheets, keep surrounding text/table rows in snippets so reviewers can verify row alignment.
7. Do not infer missing characters or decimal points from context.
8. If OCR and text-layer extraction disagree on a numeric term, stop and require manual review.

## If LLM assistance is added later

LLM assistance is allowed only as a candidate generator, never as source of truth.

LLM-assisted extraction must follow these rules:

1. The LLM prompt must include this checklist or an equivalent strict evidence policy.
2. The LLM output must be structured as candidate terms plus citations into page text.
3. Every candidate must be post-validated against raw page text by deterministic code.
4. Any candidate without an exact or reviewable page/snippet match must be discarded or marked `needs_review`.
5. The LLM must not fill gaps using prior knowledge, conventions, or assumptions.
6. The final contract JSON must store deterministic evidence, not only LLM explanations.
7. The review report must say when LLM assistance was used.
8. LLM-assisted output must remain `status: needs_review` until human approval.

## Human review checklist

Before marking a contract reviewed, confirm:

- Issuer legal name.
- CB ISIN or pending identifier status.
- Issue size and currency.
- Settlement currency.
- Stock currency.
- Denomination.
- Issue/offer price.
- Pricing/closing/issue dates.
- Maturity date.
- Maturity redemption price.
- Coupon rate and frequency.
- Underlying ticker/security.
- Initial conversion price.
- Fixed conversion FX and units/convention.
- Conversion start and end dates.
- Soft-call trigger, observation window, and dates.
- Put rights and investor put dates.
- Clean/dirty quote convention.
- Call/put/redemption schedules.
- Multi-series separation, if applicable.
- Raw PDF checksum/source path.
- Evidence matrix completeness and any remaining evidence gaps.

## Verification commands

Run from `the repository root`:

```bash
python3 -m unittest discover -s tests -q
python3 -m compileall -q cb_terminal tests
```

For PDF-backed extraction, use the project venv:

```bash
.venv/bin/python -m cb_terminal.cli.prospectus_auto_ingest --project-root .
```

When testing parser changes, prefer a temporary output directory first and verify that every populated scalar term has `source_review.term_evidence` with page/snippet evidence.
