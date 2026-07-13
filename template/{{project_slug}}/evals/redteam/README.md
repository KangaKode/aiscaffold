# Red-team config (`redteam.yaml`)

A **starter** [promptfoo](https://promptfoo.dev) config for replaying the shared
adversarial corpus against a live LLM provider. It is opt-in and runs nothing by
default — `pytest` and `scripts/validate_generated.sh` never invoke it.

## Honest scope

- This is a **configuration artifact plus this README**, not a wired-up CI job
  and not a security benchmark. It gives you a starting point; you own the
  provider, the refusal rubric, and the pass/fail bar.
- The deterministic Layer 1–2 regression coverage already lives in
  `evals/tasks/test_injection_defense_golden.py` (no LLM, runs in CI). This
  red-team config is the **Layer 3 / live-model** counterpart you run manually.

## ⚠️ Data egress + cost

Running this **sends the adversarial payloads to the provider you configure** —
a real, billed network call to a third-party LLM API, with attack strings
leaving your machine. Do not run it against an endpoint or account where that is
not acceptable.

## Setup

1. **Pin promptfoo** (the config is authored against the `0.118.x` line):

   ```bash
   npx promptfoo@0.118 --version
   ```

2. **Generate the test-case CSV from the shared corpus** (keeps one payload
   source — no copies). From the project root:

   ```bash
   python - <<'PY'
   import csv
   from tests.adversarial_payloads import INJECTION_PAYLOADS, POISONING_PAYLOADS
   from tests.adversarial_payloads_open import OPEN_CORPUS
   rows = []
   for cat, payloads in INJECTION_PAYLOADS.items():
       rows += [(f"{cat}[{i}]", p) for i, p in enumerate(payloads)]
   rows += [(f"poisoning.{k}", v) for k, v in POISONING_PAYLOADS.items()]
   for cat, seeds in OPEN_CORPUS.items():
       rows += [(f"{cat}[{i}]", p) for i, (p, _m) in enumerate(seeds)]
   with open("evals/redteam/redteam_cases.csv", "w", newline="") as f:
       w = csv.writer(f); w.writerow(["ref", "prompt"]); w.writerows(rows)
   print(f"wrote {len(rows)} cases")
   PY
   ```

   `redteam_cases.csv` is a generated artifact — do not commit it (it is a copy
   of curated adversarial strings; keep the source of truth in `tests/`).

3. **Fill in `providers:`** in `redteam.yaml` (a raw model or your gateway
   endpoint) and **tune `defaultTest.assert`** to your refusal contract.

4. **Run:**

   ```bash
   npx promptfoo@0.118 eval -c evals/redteam/redteam.yaml
   npx promptfoo@0.118 view    # inspect results locally
   ```

## Provenance

The open-corpus seeds are vendored under CC-BY-4.0 from NVIDIA's Nemotron Safety
Guard Dataset v3 — see `tests/fixtures/ATTRIBUTION.md` and
`tests/fixtures/provenance.json`.
