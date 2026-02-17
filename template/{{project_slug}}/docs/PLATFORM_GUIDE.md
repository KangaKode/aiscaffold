# Platform Deployment Guide

How to deploy this scaffold as a shared AI platform where multiple teams connect their own agents, with tenant isolation, RBAC, and cross-team agent sharing.

---

## Architecture Overview

```
                        ┌─────────────────────────────────┐
                        │      Shared Platform (you)       │
                        │                                   │
  Team A ──────────────▶│  API Gateway                     │
  (3 private agents)    │    ├── AuthContext (tenant, role) │
                        │    ├── Agent Registry             │
  Team B ──────────────▶│    │    ├── Team A agents (private)│
  (2 public agents)     │    │    ├── Team B agents (public) │
                        │    │    └── Core safety agents     │
  Team C ──────────────▶│    ├── Round Table Engine          │
  (sensitive, isolated) │    ├── Chat Orchestrator           │
                        │    ├── Evidence Enforcement        │
                        │    └── Learning System             │
                        └─────────────────────────────────┘
```

---

## Step 1: Enable Multi-Tenancy

The scaffold ships with `AuthContext` that propagates `tenant_id` to all routes. By default, everything is `"default"`. To enable real multi-tenancy:

### 1a. Replace API key auth with JWT/OIDC

Edit `src/<project>/api/middleware/auth.py`. Replace the API key logic in `verify_api_key` with your identity provider:

```python
async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security_scheme),
) -> AuthContext:
    # Replace this with your identity provider
    # Example: decode JWT, extract tenant_id and role
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing credentials")

    payload = decode_jwt(credentials.credentials)  # Your JWT decoder
    return AuthContext(
        api_key=credentials.credentials,
        user_id=payload["sub"],
        tenant_id=payload["org_id"],      # Maps to tenant isolation
        # Add custom fields as needed:
        # role=payload.get("role", "viewer"),
    )
```

Every route in the system already receives `AuthContext` -- no other changes needed for tenant identification.

### 1b. Add role to AuthContext

Edit the `AuthContext` dataclass in `auth.py`:

```python
@dataclass
class AuthContext:
    api_key: str | None = None
    user_id: str = "anon"
    tenant_id: str = "default"
    role: str = "viewer"  # Add this: "admin", "member", "viewer"
```

---

## Step 2: Add RBAC (Role-Based Access Control)

Create a permission check dependency. Add to `api/middleware/auth.py`:

```python
from functools import wraps

ROLE_HIERARCHY = {"admin": 3, "member": 2, "viewer": 1}

def require_role(minimum_role: str):
    """FastAPI dependency that enforces a minimum role."""
    def dependency(auth: AuthContext = Depends(verify_api_key)):
        user_level = ROLE_HIERARCHY.get(auth.role, 0)
        required_level = ROLE_HIERARCHY.get(minimum_role, 0)
        if user_level < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Requires {minimum_role} role (you have {auth.role})",
            )
        return auth
    return dependency
```

Then use it on sensitive routes:

```python
# Anyone can chat
@router.post("/chat")
async def send_message(auth: AuthContext = Depends(verify_api_key)): ...

# Only members can submit round table tasks
@router.post("/round-table/tasks")
async def submit_task(auth: AuthContext = Depends(require_role("member"))): ...

# Only admins can register agents
@router.post("/agents")
async def register_agent(auth: AuthContext = Depends(require_role("admin"))): ...
```

---

## Step 3: Register Team Agents with Visibility

When a team registers their agents, set `visibility` and `tenant_id`:

### Local agents (Python, running in the platform)

```python
# In gateway.py or a team-specific startup script
from src.<project>.agents.registry import AgentEntry

# Team A: private agents (only Team A can use them)
registry.register_local(
    TeamAAnalyst(llm_client=llm_client),
    capabilities=["compliance"],
)
# Manually set visibility after registration
entry = registry.get_entry("team_a_analyst")
entry.visibility = "team"
entry.tenant_id = "team_a"

# Team B: public agents (everyone can use them)
registry.register_local(
    TeamBReviewer(llm_client=llm_client),
    capabilities=["code_review"],
)
# Public by default -- visible to all tenants
```

### Remote agents (any language, running externally)

Teams register their agents via the API:

```bash
# Team C registers a private, sensitive agent
curl -X POST https://platform.example.com/api/v1/agents \
  -H "Authorization: Bearer $TEAM_C_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "incident_responder",
    "domain": "security incident analysis",
    "base_url": "https://team-c-internal.example.com",
    "capabilities": ["incident_response", "forensics"]
  }'
```

To make the registration respect tenant isolation, update the `register_agent` route to set tenant_id and visibility from the auth context:

```python
# In api/routes/agents.py, after registry.register_remote():
entry = registry.get_entry(registration.name)
entry.tenant_id = auth.tenant_id
entry.visibility = registration.visibility or "team"  # Default to team-private
```

### Visibility rules

| Visibility | Who can see it | Who can use it in round tables |
|------------|----------------|-------------------------------|
| `public` | All tenants | Any team's chat or round table |
| `team` | Same tenant only | Only the registering team |
| `private` | Registering user only | Only the specific user |

Use `registry.list_for_tenant(auth.tenant_id)` in routes instead of `registry.get_all()` to enforce visibility.

---

## Step 4: Isolate Sensitive Teams

For a team like Team C that handles sensitive data (security incidents, legal, HR):

### Data isolation

Sessions, round table results, and transcript search are already keyed by `{tenant_id}:{user_id}:{session_id}` in the chat routes. To complete isolation:

1. **Round table results**: Key the `_results_cache` by `auth.tenant_id`:
   ```python
   cache_key = f"{auth.tenant_id}:{task_id}"
   ```

2. **Transcript search**: The `TranscriptIndexer` stores `tenant_id` in metadata. Filter search results:
   ```python
   results = indexer.search(query=q, ...)
   results.results = [r for r in results.results
                       if r.metadata.get("tenant_id", "default") == auth.tenant_id]
   ```

3. **Feedback and trust**: Already scoped by `project_id` in all database tables. Map `auth.tenant_id` to `project_id` when creating trackers.

### Agent isolation

Set `visibility="private"` or `"team"` on all of Team C's agents. Update the round table to only include agents visible to the requesting tenant:

```python
# In the submit_task route, before creating the RoundTable:
visible_agents = registry.list_for_tenant(auth.tenant_id)
agents = [e.agent for e in visible_agents if e.healthy]
```

### LLM isolation (optional)

If Team C needs separate LLM credentials (different API key, different model):

```python
# Per-tenant LLM client
tenant_llm_clients = {
    "team_c": create_client(api_key=os.environ["TEAM_C_API_KEY"]),
    "default": create_client(),
}
llm = tenant_llm_clients.get(auth.tenant_id, tenant_llm_clients["default"])
```

---

## Step 5: Connect External Team Agents Safely

When a new department wants to connect their agent to the platform:

### What they need to implement

Three HTTP endpoints (any language):

```
POST /analyze   -- Returns AgentAnalysis JSON
POST /challenge -- Returns AgentChallenge JSON
POST /vote      -- Returns AgentVote JSON
```

See `docs/TUTORIAL.md` Part 5 for the full contract.

### What the platform does automatically

- **SSRF protection**: The agent's `base_url` is validated at registration (no private IPs, no cloud metadata endpoints)
- **Response sanitization**: All agent responses are sanitized for prompt injection and size-limited
- **Evidence enforcement**: The enforcement pipeline validates the agent's analysis before it enters the challenge phase
- **Rate limiting**: Per-IP rate limits on all endpoints
- **HMAC webhooks**: For async agents, webhook payloads are signed with HMAC-SHA256

### Onboarding checklist for a new team

1. Team gets a JWT/API key with their `tenant_id` and `role` from your identity provider
2. Team builds their agent (any language) implementing the 3-endpoint protocol
3. Team registers via `POST /api/v1/agents` with their credentials
4. Platform admin sets visibility (`public` if shared, `team` if private)
5. Team's agent now participates in their round tables and chat sessions
6. Core safety agents (Skeptic, Quality, Evidence, FactChecker, Citation) automatically participate alongside the team's agents

---

## Summary: What's Built vs What You Add

| Capability | Status | Notes |
|------------|--------|-------|
| AuthContext with tenant_id | **Built** | Propagates to all 25+ routes |
| Agent visibility (public/team/private) | **Built** | `list_for_tenant()` filters by rules |
| Session isolation | **Built** | `{tenant_id}:{user_id}:{session_id}` |
| Core safety agents | **Built** | Auto-included in every round table |
| Evidence enforcement | **Built** | Runs on all agent responses |
| JWT/OIDC auth | **You add** | Replace `verify_api_key` (~20 lines) |
| RBAC role checks | **You add** | `require_role()` dependency (~15 lines) |
| Per-tenant data scoping | **You add** | Key caches by `auth.tenant_id` (~5 lines per route) |
| Per-tenant LLM clients | **You add** | Optional, for credential isolation |
| Agent marketplace UI | **You add** | `list_for_tenant()` provides the data |
