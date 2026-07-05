# Case Study: The Writers Room

The most useful test of a scaffold is whether it survives a domain it was never designed for. roundtable's deliberation, security, and governance patterns grew out of security and compliance work. This page is about what happened when I used them to build a manuscript editing platform for fiction authors.

The product is private while in active development, so this page reports what happened rather than linking to code. Numbers are self-reported as of July 2026 and stated conservatively.

## Why fiction editing, of all things

My husband is an author. Watching him work through revision cycles, I kept seeing the same problem the scaffold was built to address: a single AI assistant gives you one confident opinion, and you have no idea whether to trust it. An editor persona will praise a chapter that a continuity reader would flag in seconds. What he actually needed was a room full of specialists who disagree with each other in useful ways.

That made it a good stress test. Fiction editing is about as far from my professional background as a domain gets. There are no CVEs, no compliance requirements, no incident tickets. If the round table pattern only worked for security-shaped problems, this is where it would fall apart.

## What transferred without a fight

More than expected. The inheritance is structural rather than cosmetic.

- **The deliberation protocol.** The same phase sequence (independent analysis, then challenge, then synthesis with voting and preserved dissent) drives every full-room editing session. Thirteen editorial seats replaced the security analysts: a story architect, a character psychologist, a dialogue coach, a lore keeper for canon and continuity, a sensitivity reader, a tension and pacing analyst, and so on.
- **The safety agents.** Skeptic, Quality, Evidence, FactChecker, and Citation carried over almost unchanged. It turns out a Skeptic is just as useful when the overconfident claim is "this chapter's pacing works" as when it is "this alert is a false positive."
- **Evidence discipline.** Editing suggestions must cite the manuscript. A claim about a character contradicting her earlier motivation has to point at the actual passages, which is the same grounding rule the scaffold enforces on analytical claims, wearing a different outfit.
- **The security layer.** Prompt injection defense, SSRF-validated integrations, tenant-scoped auth, and spend ceilings all moved over. Authors upload untrusted documents all day, so the hostile-input assumption earned its keep immediately.
- **Learning and trust.** Per-specialist trust scores from accept/reject feedback shape routing, the same EMA pattern the scaffold ships.

## What the domain pushed back on

A scaffold that survives contact with a real domain should come back with scars. These were the interesting ones:

- **Challenge got smarter.** Security deliberations benefit from every agent challenging every other agent. In editing, that burned tokens debating chapters nobody disagreed about. The platform made the challenge phase conditional (skipped when independent analyses already agree) and scoped it to the seats that actually found something, plus the Skeptic.
- **Challenges became anonymous.** Peer findings are stripped of attribution before the challenge round, because named specialists deferred to high-trust colleagues instead of arguing. Identity bias exists in agent rooms too.
- **Personas matter in consumer domains.** Authors respond to "Dr. Sage, the character psychologist" in a way no one responds to "analyst_b". Each seat carries a persona, a sample question, and explicit boundaries, including instructions to hand off rather than freelance outside its domain.
- **New defenses came back upstream as candidates.** Real authors type things that look exactly like fence-break attacks, because manuscripts contain dialogue about anything. The platform added fence neutralization to the prompt guard (defanging boundary tags typed inside user content) and deterministic attribution gates that verify a quoted span actually belongs to the claimed character before an agent may cite it. Hardening born from production use is on the shortlist to fold back into this scaffold.

## Where it stands

As of July 2026, across roughly four months of active development:

- 295 commits on the platform, preceded by a 121-commit proof of concept that validated the pattern before the scaffold migration
- 2,251 backend test functions across 150 test files, plus frontend unit and Playwright end-to-end suites
- A CI pipeline where six jobs (guardrails, architecture and unit tests, lint and security scans, a Postgres row-level-security lane, frontend build and tests) all gate the seventh, the deploy
- More than 95 releases to Fly.io, running as separate app and worker processes, with automated offsite backup and restore-verification workflows
- 182 tracked features. 33 are complete, 37 in progress, the rest roadmap. This is a product in the middle of being built.

Production here means deployed and in daily use by its first demanding user, my husband, working on real manuscripts. It does not mean a public launch.

## What this proves, and what it does not

It proves the scaffold's core bet: the deliberation protocol, the safety agents, the evidence discipline, and the security posture are domain-agnostic. Swapping analytical seats for editorial ones took configuration and prompt work. No architectural surgery was needed. And the feedback loop ran in both directions, since daily use in a foreign domain produced security hardening the scaffold did not have.

It does not prove independent adoption. I built the scaffold and I built the platform, so this is a first-party transfer test. The platform is unfinished and its numbers are self-reported. If you build something on roundtable in your own domain, that would be the evidence this page cannot provide. I would like to hear about it.
