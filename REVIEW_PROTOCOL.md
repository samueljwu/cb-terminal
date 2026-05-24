# Review Protocol

Use this checklist before treating pricing or UI changes as ready:

1. Keep prospectus contract terms, market rows, assumptions, and pricing results separate.
2. Verify cross-currency behavior explicitly, including fixed conversion FX vs market FX.
3. Add or update `unittest` coverage before changing production code.
4. Run:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q cb_terminal tests
```

5. For API/UI changes, smoke-test the stdlib server:

```bash
python3 -m cb_terminal.cli.serve --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/api/batch-price?steps=20'
```

6. Do not commit secrets, private `.env` files, or raw prospectuses/exports unless intentionally sanitized.
