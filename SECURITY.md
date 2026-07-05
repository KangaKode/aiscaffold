# Security Policy

## Reporting a Vulnerability

Please report suspected vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/KangaKode/roundtable/security/advisories/new) (Security tab -> "Report a vulnerability"). Do not open a public issue for security reports.

Include what you can: affected file or endpoint, reproduction steps, and impact assessment. Reports against both the scaffold itself and the code it generates are in scope.

## Response Expectations

This is a single-maintainer open-source project. Reports are acknowledged on a best-effort basis, normally within 7 days. Confirmed vulnerabilities are fixed on `main` and noted in the advisory; there is no embargo coordination process or bug bounty.

## Supported Versions

Only the latest state of `main` is supported. This is a project *template*: generated projects vendor the code at generation time and do not receive automatic updates. If a vulnerability is fixed in the template, projects generated earlier must apply the fix themselves (or re-generate and diff). Significant security fixes are called out in commit messages and release notes to make that practical.

Dependency updates for the scaffold's own tooling are automated via Dependabot; generated projects manage their own dependency lifecycle.

## Threat Model (Summary)

Generated projects assume hostile input on every boundary: user messages, remote agent responses, MCP tool output, and persisted registry/learning data are all treated as untrusted. Controls include a 3-layer prompt-injection defense, SSRF-validated agent registration, per-agent identity tokens with scope filtering and rate limits, tenant-scoped visibility, metadata-only audit trails, and fail-closed behavior when a control cannot run. Every control is mapped to its implementation and tests -- and, just as deliberately, to what it does **not** claim to do.

Full details: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) and the generated project's [GOVERNANCE.md](template/%7B%7Bproject_slug%7D%7D/docs/GOVERNANCE.md) (single source of truth for the capability matrix and non-claims).
