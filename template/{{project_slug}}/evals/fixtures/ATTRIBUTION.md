# Third-party data attribution (evals)

The evals golden set and red-team config exercise the adversarial corpus that
ships in `tests/` (`tests/adversarial_payloads.py` hand-crafted + the vendored
open subset in `tests/adversarial_payloads_open.py`). The vendored open subset
carries third-party data under CC-BY-4.0.

- **Dataset:** NVIDIA *Nemotron Safety Guard Dataset v3* (CultureGuard),
  © NVIDIA Corporation.
- **License:** CC-BY-4.0 — <https://creativecommons.org/licenses/by/4.0/>.
- **Source:** <https://huggingface.co/datasets/nvidia/Nemotron-Safety-Guard-Dataset-v3>
- **Full attribution, pinned revision, modification notice, and provenance
  schema:** `tests/fixtures/ATTRIBUTION.md` and `tests/fixtures/provenance.json`
  (single source of truth; not duplicated here because the `tests/` tree ships
  in every profile while `evals/` does not).
