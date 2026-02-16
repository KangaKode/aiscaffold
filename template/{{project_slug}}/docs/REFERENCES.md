# References

Industry research and patterns that informed the aiscaffold architecture.

---

## Anthropic

- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) -- Session lifecycle, startup/cleanup rituals, three-layer external state
- [Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf) -- Progressive disclosure, skill architecture, degrees of freedom
- [Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system) -- Orchestrator-worker pattern, hub-and-spoke, separate context windows
- [Demystifying Evals for AI Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) -- Capability vs regression evals, pass@k, graduation pattern, grader types
- [Prompt Caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) -- cache_control for token savings on stable prompt prefixes

## OpenAI

- [Harness Engineering](https://openai.com/index/harness-engineering/) -- Item/Turn/Thread model, approval gates, human-in-the-loop patterns

## Community

- [subagents.cc](https://subagents.cc/browse) -- Agent catalog and role definitions

---

## Key Principles (from the research above)

1. **Encode taste as enforceable invariants** -- Capture preferences once, enforce everywhere
2. **Grade outcomes, not paths** -- Evaluate what the agent produced, not the steps it took
3. **Three-layer external state** -- Task list (JSON) + progress notes + git history
4. **Hub-and-spoke, not mesh** -- Agents report to orchestrator, never to each other
5. **Separate context windows** -- Each agent gets its own LLM call (80% of performance)
6. **Start with 20-50 eval tasks** -- Drawn from real failures, not hypotheticals
7. **Security at boundaries** -- Parse and validate all external input at the boundary
