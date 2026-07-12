# Third-party data attribution

This directory and `tests/adversarial_payloads_open.py` vendor a small, curated
subset of an open safety dataset for adversarial regression testing. The seeds
are **data, never instructions** (see `tests/test_adversarial_open_corpus.py`,
which asserts they are import-isolated from every runtime module).

## Nemotron Safety Guard Dataset v3 (CultureGuard)

- **Creator / title:** NVIDIA Corporation, NeMo Guardrails team — *Nemotron
  Safety Guard Dataset v3* (formerly *Nemotron Content Safety Dataset
  Multilingual v1*).
- **Copyright:** © NVIDIA Corporation.
- **License:** Creative Commons Attribution 4.0 International (CC-BY-4.0) —
  full text at <https://creativecommons.org/licenses/by/4.0/legalcode>,
  summary at <https://creativecommons.org/licenses/by/4.0/>.
- **Source:** <https://huggingface.co/datasets/nvidia/Nemotron-Safety-Guard-Dataset-v3>
- **Version pinned:** immutable Hugging Face dataset commit
  `a3f7ecb3433d1933701a83f18de16c36934a7f51` (recorded per-seed in
  `provenance.json` as `upstream_revision`).
- **Changes made (CC-BY-4.0 §3(a) modification notice):** we vendored a curated
  **subset** of 40 prompt strings (out of ~515k), selected under the curation
  rubric in the fixture module docstring (no CSAM-adjacent content, no
  operational uplift, no real PII/credentials, refusal-eliciting preferred).
  Prompt text is reproduced verbatim at the code-point level but stored in
  Python source as `\uXXXX` escapes; no wording was altered. Responses,
  labels, and all other dataset columns were dropped.

## `provenance.json` schema

`records[]`, one per vendored seed:

| field | meaning |
|---|---|
| `id` | upstream dataset row id |
| `category` | fixture bucket (`open_injection` / `open_multilingual` / `open_content_safety`) |
| `lang` | ISO language code of the payload |
| `dataset` | upstream dataset name |
| `source_url` | upstream dataset URL |
| `license` | `CC-BY-4.0` |
| `upstream_revision` | immutable dataset commit SHA (re-derivability anchor) |
| `sha256` | SHA-256 over the **exact** payload code points (UTF-8, no normalization) |

The hash attests the **payload bytes only** — not `category`, `lang`, or
`source_url`. `source_url` / `upstream_revision` are recorded metadata and are
**never fetched at test time**; the provenance test is fully offline.
