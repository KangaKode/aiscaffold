"""Eval test fixtures -- mock LLM, eval config, temp workspace."""

import pytest
from unittest.mock import AsyncMock


@pytest.fixture
def mock_llm():
    """Mock LLM client that returns configurable responses without API calls."""
    client = AsyncMock()
    client.call.return_value = AsyncMock(
        content='{"observations": [], "recommendations": []}',
        model="mock-model",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
    )
    return client


@pytest.fixture
def sample_task():
    """Sample round table task for testing."""
    from {{ project_slug }}.orchestration.round_table import RoundTableTask

    return RoundTableTask(
        id="eval_test_001",
        content="Analyze this sample text for quality and accuracy.",
        context={"source": "eval_fixture"},
        constraints=["Must cite evidence", "Must include confidence score"],
    )
