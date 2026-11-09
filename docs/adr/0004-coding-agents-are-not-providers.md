# ADR-0004: Coding agents (Claude Code, Codex) are delegated to, not used as providers

Status: accepted (2026-11-09)

## Context
The blueprint lists Codex and Claude Code next to OpenAI, Anthropic and Ollama as "AI
providers". They are autonomous agents with their own tool loops, workspaces and
approval flows. They are not text-completion endpoints.

## Decision
`LLMProvider` stays text in, text out. Phase 7 introduces a separate `CodingAgent`
abstraction (start a task in a repo, stream progress, collect the diff, run tests,
report). Each agent is integrated only through its official CLI or SDK. FRIDAY
orchestrates and reports; permission rules (Phase 6) govern what an agent may do,
for example pushing requires confirmation.

## Consequences
- The conversation engine stays simple and vendor-neutral.
- The "Ask Claude to fix the reporting issue" flow from the blueprint is modelled as a
  delegated task with its own status, not as a chat message.
