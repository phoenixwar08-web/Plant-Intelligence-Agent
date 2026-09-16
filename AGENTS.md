# Plant Intelligence Agent

## Project and control boundary

This project builds a long-running plant-care agent that turns real observations into structured, traceable care proposals and feedback. The stable control chain is:

```text
Phase1 → Phase2 → Phase3 → ActuatorLayer → MQTT → ESP32
```

Phase3 remains the final safety and execution authority. `state.v1` is complete and read-only; later Agent modules must not bypass the chain.

## Start here, then read only what the task needs

Read this file and the assigned GitHub Issue first. Then use the smallest relevant route:

| Task | Read next |
| --- | --- |
| Ordinary implementation, test, or Review | `docs/AGENT_DEVELOPMENT_RULES.md` |
| State or shared protocol | `docs/PROTOCOLS_AND_BOUNDARIES.md` |
| Vision, Strategy, Episode, Feedback, Replay, or cross-module design | `docs/SYSTEM_ARCHITECTURE.md` and, when a protocol is involved, `docs/PROTOCOLS_AND_BOUNDARIES.md` |
| Phase3 or execution chain | `services/soil3/phase3/AGENTS.md` |

Read the affected code before editing it. A local `AGENTS.md` always applies to its directory.

## Scope rules

- The Issue is the modification boundary. Reuse existing capability first; add a module only when the current structure cannot carry the authorized work.
- Make the smallest change that solves the stated problem. Do not refactor stable code because it looks cleaner or more advanced.
- Do not edit, move, rename, or repair unrelated modules. Record adjacent problems unless they directly block the Issue; if they do, make the smallest fix and explain it in Review.
- Do not privately change public/frozen protocols or invent absent facts, timestamps, feedback, measurements, or outcomes.
- Do not handle Issue-external production problems, production configuration, databases, or historical data without explicit authorization.

When sources conflict, use this order: explicit Issue scope → local `AGENTS.md` → this file → `PROTOCOLS_AND_BOUNDARIES.md` → `SYSTEM_ARCHITECTURE.md` → other design/history documents. An Issue never overrides Phase3 safety rules, real pump-control boundaries, production-data protection, or database-structure limits.

## Review handoff

After tests and diff review, submit **Review**—do not self-close the Issue. State:

1. What changed.
2. What was explicitly not changed.
3. Which modules were affected.
4. What was actually verified.
5. Remaining issues or risks.
