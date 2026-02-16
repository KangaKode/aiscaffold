# Glossary

Plain-language definitions for AI and agent terminology used in this project.
Aimed at team members, stakeholders, and new contributors who may not have a deep AI background.

---

| Term | Definition |
|------|-----------|
| **Agent** | A software component that receives a task, reasons about it, and produces a structured response. Each agent specializes in a specific domain (e.g., security, code quality). |
| **Round Table** | The multi-agent deliberation protocol. Agents independently analyze a task, challenge each other's findings, then vote on a synthesized recommendation. |
| **Consensus** | Agreement among agents that a recommendation is sound. Measured as the percentage of agents who vote "approve" (the approval rate). |
| **Synthesis** | The merged recommendation produced after all agents have analyzed a task and challenged each other. Combines key findings, trade-offs, and minority views. |
| **LLM (Large Language Model)** | The AI model (e.g., Claude, GPT, Gemini) that agents use to reason about tasks. The project wraps LLM calls behind a unified client. |
| **Prompt** | The text instruction sent to an LLM. Includes system instructions (role, rules) and user content (the task). |
| **Prompt Caching** | Reusing the unchanged prefix of a prompt across calls to reduce cost and latency. The system prompt is cached; only user content varies. |
| **Token** | The unit of text that LLMs process. Roughly 4 characters or 0.75 words. Cost is measured in input + output tokens. |
| **Provider** | The company hosting the LLM (Anthropic, OpenAI, Google). The project abstracts providers behind a common interface. |
| **API Gateway** | The FastAPI server that exposes HTTP endpoints for chat, round table tasks, feedback, and agent management. |
| **Orchestrator** | The component that routes user messages to relevant agents, cross-checks their responses, and synthesizes a final answer. |
| **Agent Router** | Selects which agents to consult for a given message based on domain relevance and trust scores. |
| **Trust Score** | A per-agent reliability score (0.0 to 1.0) that adjusts over time based on user feedback (accept, reject, modify). |
| **Feedback Signal** | A user reaction to agent output: accept, reject, modify, rate, dismiss, or escalate. Feeds into the learning system. |
| **User Profile** | Accumulated preferences and interaction patterns. Used to personalize agent behavior and routing. |
| **Session** | A conversation thread. Contains turns (user inputs and agent responses) and maintains context across messages. |
| **Escalation** | When the chat orchestrator determines a topic needs the full round table protocol instead of a quick chat response. |
| **SSRF (Server-Side Request Forgery)** | An attack where a user tricks the server into making requests to internal resources. The project blocks private IPs and dangerous hostnames. |
| **Rate Limiting** | Restricting the number of requests a client can make per minute to prevent abuse. |
| **Eval (Evaluation)** | An automated test that measures AI output quality -- not just "does it run" but "is the answer good." |
| **Regression Eval** | An eval that checks whether a change made things worse compared to a known-good baseline. |
| **Prompt Guard** | Input sanitization that detects and neutralizes prompt injection attempts before they reach the LLM. |
| **Artifact** | A structured output file (JSON) written by the round table after deliberation. Contains analyses, challenges, synthesis, and votes. |
| **Domain Boundary** | The rule that agents must stay within their area of expertise and explicitly flag when a question crosses into another domain. |
| **Evidence Citation** | The requirement that every agent finding must reference specific evidence from the input, not just assert conclusions. |
