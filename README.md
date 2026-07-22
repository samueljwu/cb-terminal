# CB Terminal

Local terminal built for convertible bonds.

## Quick start

Python 3.11+ is required. The core package uses the standard library; PyMuPDF is optional for text-layer PDF extraction.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[prospectus]'
.venv/bin/python -m cb_terminal.cli.serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

Run checks:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q cb_terminal tests scripts
python3 scripts/check_public_tree.py
```

## Workflow

1. Upload prospectus PDFs and market-data exports.
2. Extract evidence-backed contract terms for review.
3. Import CB quote, equity, and FX observations into generated valuation histories.
4. Price approved contracts with explicit PM assumptions.

Core modules: `domain`, `prospectus`, `storage`, `pricing`, and `web`.

Implementation details, equations, and limitations are documented in
[`VALUATION_MODEL.md`](VALUATION_MODEL.md).

References:

Cox, J. C., Ross, S. A., & Rubinstein, M. (1979). Option pricing: A simplified approach. *Journal of Financial Economics, 7*(3), 229–263. https://doi.org/10.1016/0304-405X(79)90015-1

Tsiveriotis, K., & Fernandes, C. (1998). Valuing convertible bonds with credit risk. *The Journal of Fixed Income, 8*(2), 95–102. https://doi.org/10.3905/jfi.1998.408243

## Status

Implemented: local browser UI, command bar, prospectus intake/review, raw market-data import, valuation-history generation, canonical SQLite metadata, assumptions, pricing diagnostics, and stdlib tests.

Not implemented: scanned-PDF OCR, live market-data pulls, authentication/multi-user deployment, full desk calibration, and every call/put/conversion edge case.

This is a local prototype, not a trading system. Private inputs, generated outputs, runtime databases, reports, caches, and `.venv` are ignored; `scripts/check_public_tree.py` enforces that boundary.

## Screenshot of the Terminal
<img width="1701" height="1012" alt="image" src="https://github.com/user-attachments/assets/11c99025-8368-4846-a57b-42e3aece592d" />
