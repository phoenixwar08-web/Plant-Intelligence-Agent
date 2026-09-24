# Controlled soil3 Phase3 execution adapter

This module is the narrow Day 6 handoff from an accepted
`phase3_bridge_response.v1` to the existing zero-argument
`DecisionBrain.run_cycle` interface.

It has no command-line entrypoint and never imports or constructs Phase3. A
caller may supply the existing bound `DecisionBrain.run_cycle` method only
after all Day 5 PRs are merged and the Owner has created one explicit,
short-lived approval bound to the exact Bridge request and bindings.

Before calling Phase3, the adapter checks:

- the Bridge response is accepted, reason-free, verification-only, and names
  exactly `DecisionBrain.run_cycle()` with no arguments;
- the approval is for soil3, one run only, unexpired, and bound to the exact
  request, State, Strategy, and Gate identifiers;
- the approval carries the Trace and open Episode identifiers used for later
  factual feedback.

The approval is claimed on disk before Phase3 is called. A process crash,
retry, or duplicate request therefore cannot call Phase3 twice with the same
approval. A failed Phase3 call remains consumed and records an unknown physical
outcome instead of claiming that no action happened.

The receipt stores Phase3's returned zone, action duration, plan label, notes,
and sensor reading projection. It does not copy an Agent action into Phase3,
call `manual_water`, publish MQTT, or invoke ActuatorLayer directly. Phase3
remains the final decision and safety authority.

No real scenario was approved or executed as part of this change. Unit tests
use a fake zero-argument Phase3 callable and temporary directories only.
