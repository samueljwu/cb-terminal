# Restore

This project is intentionally standalone.

1. Copy the project folder to a machine with Python 3.11+.
2. Optional but recommended for prospectus PDF text extraction: create a project-local virtual environment and install the PyMuPDF extra:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[prospectus]'
```

If `uv` is unavailable, use a normal virtualenv/pip equivalent:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[prospectus]'
```

3. From the project root, run:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m cb_terminal.cli.serve --host 127.0.0.1 --port 8000
```

For the core stdlib-only path, `python3` can be used instead of `.venv/bin/python`; direct PDF extraction from text-layer prospectuses requires the PyMuPDF-backed environment above.

4. Open `http://127.0.0.1:8000/`.

No database, Docker service, API key, or external market-data source is required for the current demo/test path. OCR for scanned/image-only prospectus PDFs is not bundled; add reviewed page-text fixtures or an OCR workflow for those files.

Raw local documents under `data/raw/` are optional and ignored by git. Shareable normalized examples live under `data/contracts/` and `tests/fixtures/`.
