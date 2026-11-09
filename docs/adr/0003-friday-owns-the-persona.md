# ADR-0003: FRIDAY owns the persona, providers only generate text

Status: accepted (2026-11-09)

## Decision
The system prompt (identity + mode overlay + channel rules) is built by
`persona/persona.py` and sent identically to every provider. Mode switches and voice
control words are parsed deterministically before any model call. Speech output goes
through a provider-independent sanitizer. Provider adapters have no persona logic.

## Consequences
- Switching providers changes capability, not character.
- Small local models may still drift in tone, so a persona-conformance evaluation per
  provider is planned for Phase 3.
