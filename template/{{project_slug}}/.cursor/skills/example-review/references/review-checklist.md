# Code Review Checklist

## Security
- [ ] No hardcoded secrets (API keys, passwords, tokens)
- [ ] Parameterized queries (no f-strings in SQL)
- [ ] User input validated at boundaries
- [ ] No eval(), exec(), pickle on untrusted data
- [ ] System/user prompt boundary maintained

## Architecture
- [ ] Dependency directions respected (lower layers don't import higher)
- [ ] Files under 500 lines
- [ ] New types defined in data layer

## Quality
- [ ] Functions have docstrings
- [ ] Logging includes [ComponentName] prefix
- [ ] Error handling with exc_info=True
- [ ] No TODO/FIXME in production code

## Testing
- [ ] New code has corresponding tests
- [ ] Test names describe what they test
- [ ] Appropriate markers (P0/P1/smoke/slow)

Guidance verified: 2026-07
