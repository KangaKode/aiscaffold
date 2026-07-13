# `tests/fixtures/`

Static test data for the adversarial regression suite. Ships in **every**
generated project (the `tests/` tree is not gated by `include_evals`).

## Heads-up for downstream scanners

`adversarial_payloads_open.py` (in the parent `tests/` directory) and the
manifest here contain **curated adversarial test strings** — jailbreak framings,
non-English injection attempts, and content-safety false-positive controls.
This is deliberate red-team fixture data. DLP / antivirus / secret-scanning
tools may flag it; that is expected. The seeds were curated under a strict
rubric (no CSAM-adjacent content, no operational uplift such as
weapons/CBRN/malware synthesis, no real PII or credentials; refusal-eliciting
prompts preferred over intrinsically harmful strings) — see the fixture module
docstring and `ATTRIBUTION.md`.

## Files

- `provenance.json` — per-seed provenance + tamper-evidence manifest (schema in
  `ATTRIBUTION.md`). The SHA-256 of each seed's exact code points is recomputed
  by `tests/test_adversarial_open_corpus.py`; **never edit a seed without
  updating its hash.**
- `ATTRIBUTION.md` — CC-BY-4.0 attribution and modification notice for the
  vendored subset.
