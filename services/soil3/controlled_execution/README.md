# Controlled soil3 Phase3 execution adapter

This module is the narrow Day 6 handoff from one persisted Shadow chain to the
existing zero-argument `DecisionBrain.run_cycle` interface.

It has no command-line entrypoint and never constructs Phase3. A caller must
supply the Trace directory and an already-created formal `DecisionBrain`
instance after the Owner has created one explicit, short-lived approval bound
to the exact persisted Bridge request and bindings. Arbitrary callables are
not accepted.

Before calling Phase3, the adapter checks:

- the Trace is loaded through `TraceStore`, remains non-executing, and its
  State, Strategy, Gate v2, dry-run Runner, Bridge, and Episode artifact
  references match the persisted file hashes and record identifiers;
- the open Episode embeds the same State, Strategy, and Gate facts;
- the public `Phase3Bridge().verify(...)` accepts those persisted upstream
  records and reproduces the exact handoff bindings and formal interface;
- the approval names the configured Owner, is for soil3 and one run only, is
  unexpired, and is bound to that exact Bridge request, Trace, and Episode.

The approval is claimed on disk before Phase3 is called. A process crash,
retry, or duplicate request therefore cannot call Phase3 twice with the same
approval. A failed Phase3 call remains consumed and records an unknown physical
outcome instead of claiming that no action happened.

The receipt stores Phase3's returned zone, action duration, plan label, notes,
and sensor reading projection. It does not copy an Agent action into Phase3,
call `manual_water`, publish MQTT, or invoke ActuatorLayer directly. Phase3
remains the final decision and safety authority.

No real scenario was approved or executed as part of this change. Unit tests
use temporary records and replace the formal class method before invocation.
