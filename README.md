# aiscaffold

**AI Project Scaffold with 2026 Best Practices**

One command to set up a new AI tool project with everything the experts recommend: progressive disclosure docs, architecture enforcement tests, 14 subagents, red team pre-commit hooks, CI/CD, eval infrastructure, and quality tracking.

---

## Quick Start

```bash
# Install
pip install copier

# Create a new project
copier copy gh:KangaKode/aiscaffold my-project --trust

# Follow the prompts: name, type, layers, LLM provider, persistence
# Then:
cd my-project
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pre-commit install
make test  # Architecture tests pass from day 1
```

---

## What You Get

Every scaffolded project includes **39 files** out of the box:

### 14 AI Subagents (`.cursor/agents/`)

| Agent | Role |
|-------|------|
| **solution-architect** | Must be consulted before any new feature is coded |
| **codebase-scout** | Searches existing code before allowing new code to be written |
| **data-flow-guardian** | Validates data paths, source of truth, transaction safety |
| **minimalist** | Prevents over-engineering and AI code bloat |
| **code-reviewer** | Quality, security, maintainability review |
| **red-team** | Adversarial pre-commit security gate (BLOCKS on findings) |
| **security-hardener** | Blue team -- proactive defensive security |
| **prompt-engineer** | 2026 Anthropic Skills patterns for prompt design |
| **ai-engineer** | Multi-agent architecture and orchestration |
| **test-architect** | Test strategy, eval design, coverage analysis |
| **debugger** | Systematic root cause analysis |
| **project-curator** | Directory structure and root cleanliness |
| **sql-pro** | Database optimization (conditional on persistence choice) |
| **ux-researcher** | User workflow optimization (conditional on project type) |

### Architecture Enforcement

```
tests/test_architecture.py

Enforces:
- Dependency direction rules (lower layers cannot import from higher)
- File size limits (warning at 500+ lines)
- Root cleanliness (no stray files)
- Core module existence
```

Parameterized from your layer choices at init time. Passes on a fresh project with zero code written.

### Red Team Pre-Commit Hook

```
scripts/red_team_check.py

Checks before every commit:
- Hardcoded secrets (API keys, passwords, tokens)
- SQL injection (f-strings in queries)
- Dangerous functions (eval, exec, pickle)
- Architecture violations (forbidden imports)
- Data safety (DROP TABLE, DELETE without WHERE)
- File size limits

BLOCKING findings prevent the commit.
```

### Progressive Disclosure Knowledge Base

```
CLAUDE.md           -- Entry point for AI agents (~100 lines, links to docs/)
docs/ARCHITECTURE.md -- Canonical layering rules with enforcement details
docs/QUALITY_SCORE.md -- Per-domain quality grades
docs/TESTING_STANDARDS.md -- Testing conventions and eval guide
docs/INDEX.md        -- Registry of all documentation
```

### CI/CD Pipeline

```
.github/workflows/ci.yml

Stages:
1. P0 architecture tests (every commit)
2. P0 critical tests
3. P1 important tests
4. Full suite with coverage
5. Lint (Black, Ruff, Bandit)
```

### Makefile

```bash
make help          # Show all commands
make test          # Run all tests
make test-p0       # P0 critical tests only
make test-arch     # Architecture enforcement
make red-team      # Run red team on all source files
make lint          # Run linters
make format        # Format code
make doctor        # Full project health check
make clean         # Remove caches
```

---

## Project Types

| Type | Command | Layers | Extras |
|------|---------|--------|--------|
| Web App | `--data project_type=web-app` | data, analysis, components | NiceGUI/FastAPI |
| CLI Tool | `--data project_type=cli-tool` | data, logic | Typer |
| Multi-Agent | `--data project_type=multi-agent` | data, analysis, orchestration, specialists, prompts | LLM client, evals |
| API Service | `--data project_type=api-service` | data, service, routes | FastAPI |

---

## Architecture

```
aiscaffold/
  copier.yml            # Template configuration and questions
  README.md             # This file

  template/             # Copier template (generates project files)
    {{project_slug}}/
      .cursor/agents/   # 14 subagent definitions
      .cursor/rules/    # Example domain rule
      .github/workflows/ # CI pipeline
      docs/             # Progressive disclosure docs
      tests/            # Architecture enforcement tests
      scripts/          # Red team pre-commit hook
      ...               # Config files (pyproject.toml, Makefile, etc.)

  core/                 # Shared utilities (install via pip)
    pyproject.toml      # Package config
    src_aiscaffold/     # Python package
      cli.py            # CLI: init, doctor, add, update
      task_tracker.py   # JSON-based task tracking
      progress_notes.py # Session progress logging
      eval_harness.py   # Evaluation infrastructure
```

---

## CLI (Optional)

Install the core package for CLI access and shared utilities:

```bash
cd core && pip install -e .

# Create new project
aiscaffold init my-project

# Check project health
aiscaffold doctor

# Add modules
aiscaffold add evals        # Eval infrastructure
aiscaffold add agent:my-bot # New subagent
aiscaffold add layer:api    # New architecture layer

# Pull template updates
aiscaffold update
```

---

## Updating Existing Projects

```bash
# When the template improves, update your project:
copier update --trust

# Copier stores your answers in .copier-answers.yml
# It merges changes without re-asking questions
```

---

## Based On

Built from insights in:

- [Anthropic: Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf)
- [Anthropic: Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system)
- [Anthropic: Demystifying Evals for AI Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [OpenAI: Harness Engineering](https://openai.com/index/harness-engineering/)
- [subagents.cc](https://subagents.cc/browse) -- Agent catalog

---

## License

MIT
