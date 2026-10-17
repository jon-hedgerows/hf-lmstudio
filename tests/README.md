# Tests

Tests now exist and live in `tests/test_cli.py`. They use only the Python
standard library (`unittest` + `tempfile`), consistent with SPEC §4 (no new
runtime dependencies).

Run them with:

```shell
python -m unittest discover -s tests
```

or, if pytest is available:

```shell
pytest tests
```
