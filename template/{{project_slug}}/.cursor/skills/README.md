# Skills Directory

Skills are portable packages of expertise that teach AI agents how to handle specific tasks.

## Structure

```
skills/
  my-skill/
    SKILL.md         <- Required: Instructions with YAML frontmatter
    scripts/         <- Optional: Executable code
    references/      <- Optional: Documentation loaded as needed
    assets/          <- Optional: Templates, fonts, icons
```

## How to Create a Skill

1. Create a folder with a descriptive kebab-case name
2. Add `SKILL.md` with YAML frontmatter (name, description, trigger phrases)
3. Add reference files if the skill needs additional context
4. The body of SKILL.md should be under 500 lines

## Skills in This Template

| Skill | Purpose |
|-------|---------|
| `example-review/` | Systematic code review with security, quality, and architecture checks (also serves as the format example) |
| `phased-delivery/` | Four-phase multi-agent workflow (brainstorm, tickets, plan, parallel build) for features too large for a single session |

## Example

See `example-review/SKILL.md` for a working example.

## Key Principles

- **Progressive disclosure**: YAML frontmatter is always loaded; body loads only when relevant
- **Degrees of freedom**: High for creative tasks, Low for fragile operations
- **Trigger phrases**: Include specific phrases users might say to invoke this skill
- **Task boundaries**: State what the skill does NOT do

## Reference

See `docs/REFERENCES.md` for the Anthropic Skills guide and other industry research.

Guidance verified: 2026-07
