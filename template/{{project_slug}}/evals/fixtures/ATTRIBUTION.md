# Third-party data attribution (evals)

## Open corpus (pointer)

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

## Public-corpus harness subsets (evals-gated)

`public_corpus_*.json` vendors offline **subsets** for Layer 1–2 measurement.
This is not an end-to-end run of either upstream benchmark and is not a
leaderboard score. See `evals/tasks/test_public_corpus_harness.py` and the
manifest for pinned SHAs and selection rubrics.

### InjecAgent

- **Upstream:** <https://github.com/uiuc-kang-lab/InjecAgent>
- **License:** MIT — <https://github.com/uiuc-kang-lab/InjecAgent/blob/main/LICENSE>
- **Pinned commit:** see `public_corpus_manifest.json` → `sources.injecagent.commit_sha`
- **Modification:** selected English-primary attacker-instruction and
  tool-response strings only; no tool runtime; curated per the selection
  rubric in the manifest (no CSAM-adjacent, no operational uplift, no real
  PII/credentials).

### AgentDojo

- **Upstream:** <https://github.com/ethz-spylab/agentdojo>
- **License:** MIT — <https://github.com/ethz-spylab/agentdojo/blob/main/LICENSE>
- **Pinned commit:** see `public_corpus_manifest.json` → `sources.agentdojo.commit_sha`
- **Modification:** injection-relevant jailbreak template strings filled with
  suite injection-task GOAL text, plus a small set of injection-vector
  defaults as pass controls. Not agent task-success scoring.

### Open-corpus continuity

- Sample of the vendored open subset above (same CC-BY-4.0 terms); pin is the
  `git hash-object` of `tests/adversarial_payloads_open.py` recorded in the
  manifest.
