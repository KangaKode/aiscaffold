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
knowledge reads. Setup registers a probe agent to exercise registration +
SSRF validation, then unregisters it IMMEDIATELY: remote registrations
persist in .aiscaffold/agents.json, so a leftover unreachable probe would
survive restarts and force every deliberation into degraded mode. The
round-table task therefore needs YOUR deployment's agents registered;
against a bare stack it reports an explicit, actionable failure.

Keep this file under 200 lines.
"""

import os
import time
import uuid

from locust import HttpUser, between, events, task

API_PREFIX = "/api/v1"
PROBE_AGENT_NAME = "load_probe_agent"
SETUP_RETRIES = 5
SETUP_RETRY_DELAY_SECONDS = 2.0


def _headers() -> dict:
    api_key = os.environ.get("API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _host(environment) -> str:
    return (environment.host or "http://localhost:8000").rstrip("/")


def _setup_request(method: str, url: str, **kwargs):
    """One setup call with retries -- the gateway may still be starting
    when Locust launches. Raises a clear message on final failure."""
    import requests

    last_error: Exception | None = None
    for attempt in range(1, SETUP_RETRIES + 1):
        try:
            return requests.request(
                method, url, headers=_headers(), timeout=10, **kwargs
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < SETUP_RETRIES:
                time.sleep(SETUP_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Could not reach the gateway at {url} after {SETUP_RETRIES} attempts "
        f"({type(last_error).__name__}: {last_error}). Is the stack up? "
        f"Start it with: make load-test (or docker-compose -f docker-compose.yml "
        f"-f docker-compose.load.yml up -d), then rerun Locust."
    )


@events.test_start.add_listener
def probe_agent_registration(environment, **_kwargs):
    """Exercise agent registration + SSRF validation once per run, then
    unregister the probe in the same hook so it never participates in
    deliberations (a persisted unreachable agent would degrade every
    round-table run, during and after load testing)."""
    host = _host(environment)
    response = _setup_request(
        "POST",
        f"{host}{API_PREFIX}/agents",
        json={
            "name": PROBE_AGENT_NAME,
            "domain": "load testing",
            # .invalid never resolves, so registration passes SSRF checks
            # without pointing at anything reachable.
            "base_url": "http://load-probe.invalid:9",
            "capabilities": ["load"],
        },
    )
    if response.status_code not in (200, 409):
        raise RuntimeError(
            f"Probe agent registration failed ({response.status_code}): "
            f"{response.text}. Check API_KEY and gateway logs."
        )

    cleanup = _setup_request(
        "DELETE", f"{host}{API_PREFIX}/agents/{PROBE_AGENT_NAME}"
    )
    if cleanup.status_code not in (200, 404):
        raise RuntimeError(
            f"Probe agent cleanup failed ({cleanup.status_code}): "
            f"{cleanup.text}. Remove '{PROBE_AGENT_NAME}' manually via "
            f"DELETE {API_PREFIX}/agents/{PROBE_AGENT_NAME} before running "
            f"deliberations."
        )


@events.test_stop.add_listener
def cleanup_probe_agent(environment, **_kwargs):
    """Belt-and-braces: make sure the probe is gone when the run ends
    (best-effort -- it was already removed in test_start)."""
    try:
        _setup_request(
            "DELETE", f"{_host(environment)}{API_PREFIX}/agents/{PROBE_AGENT_NAME}"
        )
    except RuntimeError:
        pass  # gateway already down; nothing persisted beyond test_start


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
        """Tier 3: full deliberation (expensive; rare in the mix).

        Requires agents YOUR deployment registered. A bare stack has
        none, so the gateway returns 400 -- surfaced here as an explicit
        failure telling you what to do, not a raw status code.
        """
        with self.client.post(
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
            catch_response=True,
        ) as response:
            if response.status_code == 400 and "agent" in response.text.lower():
                response.failure(
                    "No agents registered: register your deployment's agents "
                    "before load-testing deliberations"
                )

    @task(2)
    def list_corrections(self):
        """Knowledge reads (also exercises the extraction guard path)."""
        self.client.get(
            f"{API_PREFIX}/corrections",
            headers=_headers(),
            name="/corrections",
        )
