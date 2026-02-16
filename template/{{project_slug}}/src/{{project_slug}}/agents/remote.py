"""
RemoteAgent -- Adapter that wraps an HTTP endpoint as an AgentProtocol implementation.

The RoundTable sees no difference between a local Python agent and a RemoteAgent.
External agents in any language (TypeScript, Go, Rust, etc.) implement 3 HTTP endpoints:

  POST {base_url}/analyze   -> AnalysisResponse JSON
  POST {base_url}/challenge -> ChallengeResponse JSON
  POST {base_url}/vote      -> VoteResponse JSON

This adapter handles the HTTP calls, timeouts, retries, and JSON conversion.

Security:
  - All agent responses are sanitized (null bytes stripped, size-limited)
  - Prompt injection patterns in agent output are detected and logged
  - Response size is capped to prevent memory exhaustion
  - String fields are truncated to safe limits

Reference: src/api/models/requests.py for the contract.
"""

import logging
from dataclasses import asdict
from typing import Any

import httpx

from ..orchestration.round_table import (
    AgentAnalysis,
    AgentChallenge,
    AgentVote,
    RoundTableTask,
    SynthesisResult,
)
from ..security.prompt_guard import detect_injection_attempt, sanitize_for_prompt

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_RETRIES = 2
MAX_RESPONSE_BYTES = 5_000_000
MAX_FIELD_LENGTH = 50_000


class RemoteAgent:
    """
    Adapter: wraps an HTTP endpoint as an AgentProtocol implementation.

    Usage:
        agent = RemoteAgent(
            name="ts_analyzer",
            domain="code analysis",
            base_url="http://localhost:3000",
            api_key="secret",
        )
        # Pass to RoundTable alongside local agents:
        rt = RoundTable(agents=[local_agent, agent], config=config, llm_client=llm)
    """

    def __init__(
        self,
        name: str,
        domain: str,
        base_url: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        mode: str = "sync",
    ):
        self._name = name
        self._domain = domain
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._mode = mode
        self._interaction_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def interaction_count(self) -> int:
        return self._interaction_count

    def _headers(self) -> dict[str, str]:
        """Build request headers including auth if configured."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _sanitize_string(self, value: str, field_name: str = "field") -> str:
        """Sanitize a string field from an external agent response."""
        sanitized = sanitize_for_prompt(value, max_length=MAX_FIELD_LENGTH)
        injections = detect_injection_attempt(sanitized)
        if injections:
            logger.warning(
                f"[RemoteAgent:{self._name}] Prompt injection patterns detected "
                f"in {field_name}: {injections}"
            )
        return sanitized

    def _sanitize_dict_list(self, items: list[dict], context: str) -> list[dict]:
        """Sanitize a list of dicts from an external agent response."""
        sanitized = []
        for item in items[:100]:
            clean = {}
            for key, val in item.items():
                if isinstance(val, str):
                    clean[key] = self._sanitize_string(val, f"{context}.{key}")
                else:
                    clean[key] = val
            sanitized.append(clean)
        return sanitized

    async def _post(self, endpoint: str, payload: dict) -> dict:
        """Send POST request with retries, size limits, and structured error handling."""
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        url, json=payload, headers=self._headers()
                    )
                    response.raise_for_status()

                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise ValueError(
                            f"Response from {self._name} exceeds "
                            f"{MAX_RESPONSE_BYTES} byte limit"
                        )

                    self._interaction_count += 1
                    return response.json()
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    f"[RemoteAgent:{self._name}] Timeout on {endpoint} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES + 1})"
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.error(
                    f"[RemoteAgent:{self._name}] HTTP {e.response.status_code} "
                    f"from {url}: {e.response.text[:200]}"
                )
                break
            except httpx.ConnectError as e:
                last_error = e
                logger.error(
                    f"[RemoteAgent:{self._name}] Connection failed to {url}: {e}"
                )
                break

        raise ConnectionError(
            f"RemoteAgent '{self._name}' failed on {endpoint} "
            f"after {MAX_RETRIES + 1} attempts: {last_error}"
        )

    async def analyze(self, task: RoundTableTask) -> AgentAnalysis:
        """POST {base_url}/analyze with task JSON, return sanitized AgentAnalysis."""
        payload = {
            "task_id": task.id,
            "content": task.content,
            "context": task.context,
            "constraints": task.constraints,
        }
        data = await self._post("analyze", payload)
        return AgentAnalysis(
            agent_name=self._name,
            domain=self._domain,
            observations=self._sanitize_dict_list(
                data.get("observations", []), "analyze.observations"
            ),
            recommendations=self._sanitize_dict_list(
                data.get("recommendations", []), "analyze.recommendations"
            ),
            confidence=min(max(float(data.get("confidence", 0.0)), 0.0), 1.0),
            raw_response=sanitize_for_prompt(str(data), max_length=MAX_FIELD_LENGTH),
        )

    async def challenge(
        self, task: RoundTableTask, other_analyses: list[AgentAnalysis]
    ) -> AgentChallenge:
        """POST {base_url}/challenge with task + analyses JSON, return sanitized."""
        payload = {
            "task_id": task.id,
            "content": task.content,
            "other_analyses": [asdict(a) for a in other_analyses],
        }
        data = await self._post("challenge", payload)
        return AgentChallenge(
            agent_name=self._name,
            challenges=self._sanitize_dict_list(
                data.get("challenges", []), "challenge.challenges"
            ),
            concessions=self._sanitize_dict_list(
                data.get("concessions", []), "challenge.concessions"
            ),
        )

    async def vote(
        self, task: RoundTableTask, synthesis: SynthesisResult
    ) -> AgentVote:
        """POST {base_url}/vote with task + synthesis JSON, return sanitized."""
        payload = {
            "task_id": task.id,
            "content": task.content,
            "synthesis": asdict(synthesis),
        }
        data = await self._post("vote", payload)
        dissent = data.get("dissent_reason")
        if isinstance(dissent, str):
            dissent = self._sanitize_string(dissent, "vote.dissent_reason")
        return AgentVote(
            agent_name=self._name,
            approve=bool(data.get("approve", False)),
            conditions=[
                self._sanitize_string(c, "vote.condition")
                for c in data.get("conditions", [])[:20]
            ],
            dissent_reason=dissent,
        )

    async def health_check(self) -> bool:
        """Check if the remote agent is reachable."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self._base_url}/health", headers=self._headers()
                )
                return response.status_code == 200
        except Exception as e:
            logger.debug(f"[RemoteAgent:{self._name}] Health check failed: {e}")
            return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize agent info for registry persistence.

        SECURITY: API keys are NEVER persisted to disk. They are stored as
        environment variable references (AGENT_{NAME}_API_KEY) and loaded
        at runtime. Only the env var name is persisted.
        """
        api_key_env = f"AGENT_{self._name.upper()}_API_KEY"
        return {
            "name": self._name,
            "domain": self._domain,
            "base_url": self._base_url,
            "api_key_env": api_key_env,
            "timeout": self._timeout,
            "mode": self._mode,
            "agent_type": "remote",
        }
