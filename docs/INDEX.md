# Documentation Index

This documentation is for reviewers and builders evaluating the `roundtable` scaffold itself. Generated projects receive their own `docs/` directory with project-specific values rendered by Copier.

## Evaluating This Project? Read These First

| Question | Document |
|----------|----------|
| What is the threat model, and what is honestly claimed vs. not? | [SECURITY_MODEL.md](SECURITY_MODEL.md) |
| Which controls exist, and where are the code and tests for each? | [GOVERNANCE.md](../template/%7B%7Bproject_slug%7D%7D/docs/GOVERNANCE.md) -- capability matrix mapping every control to its implementation and tests, plus known limitations and non-claims |
| Does the scaffold hold up outside its home domain? | [CASE_STUDY.md](CASE_STUDY.md) |

## Getting Started

| Goal | Document |
|------|----------|
| Build and run a generated project | [TUTORIAL.md](TUTORIAL.md) |
| Share a non-technical overview | [TEAM_OVERVIEW.md](TEAM_OVERVIEW.md) |
| Look up terminology | [GLOSSARY.md](GLOSSARY.md) |

## Architecture & Design

| Goal | Document |
|------|----------|
| Understand the system design | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Explore the full feature reference | [FEATURES.md](FEATURES.md) |
| Connect an external agent | [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md) |
| See the scaffold applied in another domain | [CASE_STUDY.md](CASE_STUDY.md) |

## Security & Governance

| Goal | Document |
|------|----------|
| Evaluate the security posture | [SECURITY_MODEL.md](SECURITY_MODEL.md) |
| Evaluate capability claims (control -> implementation -> tests) | [GOVERNANCE.md](../template/%7B%7Bproject_slug%7D%7D/docs/GOVERNANCE.md) |
| Report a vulnerability | [SECURITY.md](../SECURITY.md) |
| Understand the gated AI development workflow | [DEVELOPMENT_PROCESS.md](DEVELOPMENT_PROCESS.md) |

## Operations & Lifecycle

| Goal | Document |
|------|----------|
| Update a generated project to a newer template release | [UPDATING.md](UPDATING.md) |
| Deploy as a multi-team platform | [PLATFORM_GUIDE.md](PLATFORM_GUIDE.md) |
| Hand off a roundtable-based POC | [ROUNDTABLE_HANDOFF.md](ROUNDTABLE_HANDOFF.md) |

## Design References

| Document | Purpose |
|----------|---------|
| [REFERENCES.md](REFERENCES.md) | Research sources behind the scaffold design |
| [EVAL_SCALING_GUIDE.md](EVAL_SCALING_GUIDE.md) | Evaluation strategy and scaling model |
