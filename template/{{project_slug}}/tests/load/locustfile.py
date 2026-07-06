"""
Locust load harness -- realistic traffic mix against a running gateway.

NOT collected by pytest (tests/conftest.py excludes tests/load/), because
Locust is an optional extra, never a base dependency:

    pip install '.[load]'

Run against a stack started in load mode (mock LLM, relaxed rate limit):

    make load-test        # docker compose -f docker-compose.yml -f docker-compose.load.yml up -d
    locust -f tests/load/locustfile.py --host http://localhost:8000

Set API_KEY in the environment when the target enforces auth.

Traffic mix (weights): resolve and chat dominate (cheap tiers), round
table deliberations are rare (expensive tier), corrections listings model
knowledge reads. Setup registers one remote agent so the round-table
route has a registered participant; its base_url uses the reserved
.invalid TLD, so dispatch to it fails fast and the six core safety agents
(local, mock-LLM-driven) carry the deliberation.

Keep this file under 200 lines.
"""

import os
import uuid

from locust import HttpUser, between, events, task

API_PREFIX = "/api/v1"
LOAD_AGENT_NAME = "load_probe_agent"


def _headers() -> dict:
    api_key = os.environ.get("API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


@events.test_start.add_listener
def register_agents(environment, **_kwargs):
    """Register one remote agent so round-table tasks pass the
    'no agents registered' gate. 409 means a previous run already did."""
    import requests

    host = (environment.host or "http://localhost:8000").rstrip("/")
    response = requests.post(
        f"{host}{API_PREFIX}/agents",
        json={
            "name": LOAD_AGENT_NAME,
            "domain": "load testing",
            # .invalid never resolves: registration passes SSRF checks,
            # dispatch fails fast, core agents carry the deliberation.
            "base_url": "http://load-probe.invalid:9",
            "capabilities": ["load"],
        },
        headers=_headers(),
        timeout=10,
    )
    if response.status_code not in (200, 409):
        raise RuntimeError(
            f"Agent registration failed ({response.status_code}): {response.text}"
        )


class PlatformUser(HttpUser):
    """One simulated API client hitting all three resolution tiers."""

    wait_time = between(0.5, 2.0)

    def on_start(self):
        self.session_id = f"load-{uuid.uuid4().hex[:8]}"

    @task(3)
    def resolve(self):
        """Tier 1: single-shot resolution (cheapest path)."""
        self.client.post(
            f"{API_PREFIX}/resolve",
            json={"query": "What did the authentication logs show last week?"},
            headers=_headers(),
            name="/resolve",
        )

    @task(3)
    def chat(self):
        """Tier 2: chat orchestrator (lead agent + specialists)."""
        self.client.post(
            f"{API_PREFIX}/chat",
            json={
                "message": "Summarize the current authentication failure pattern.",
                "session_id": self.session_id,
            },
            headers=_headers(),
            name="/chat",
        )

    @task(1)
    def round_table(self):
        """Tier 3: full deliberation (expensive; rare in the mix)."""
        self.client.post(
            f"{API_PREFIX}/round-table/tasks",
            json={
                "content": (
                    "Assess the risk posture of the authentication service "
                    "given the recent failure spike."
                )
            },
            headers=_headers(),
            name="/round-table/tasks",
            timeout=120,
        )

    @task(2)
    def list_corrections(self):
        """Knowledge reads (also exercises the extraction guard path)."""
        self.client.get(
            f"{API_PREFIX}/corrections",
            headers=_headers(),
            name="/corrections",
        )
