# soil3 Phase3 Bridge V1

Bridge V1 is a verification-only adapter between an admitted Runner trace and
the existing formal Phase3 entrypoint. It independently revalidates:

1. `strategy.v1` against the exact `state.v1` snapshot;
2. the non-deny `gate.v1` binding, admission-only metadata and any exploration
   reservation;
3. the terminal `runner_state.v1` binding, ordered action copies, dry-run
   result for every step, and both no-execution flags.

Only after all checks pass does it return a `phase3_bridge_request.v1` handoff.
That request names the one permitted next interface,
`DecisionBrain.run_cycle()`, with an empty argument list. It deliberately does
not carry an action, requested pump duration, actuator command, or alternate
route. Strategy content therefore cannot become a direct device command, and a
denied/tampered/missing Gate or forged Runner trace cannot produce a handoff.

This issue does not authorize real execution. Bridge V1 has only `verify()`;
it does not import, construct, or invoke Phase3 and every response records
`phase3_called: false` and `physical_actions_performed: false`. A future,
separately authorized integration may consume the verified handoff through the
named Phase3 interface. It must not add a direct device route.

```bash
python -m services.soil3.phase3_bridge.service \
  --state state.json --strategy strategy.json --gate gate.json --runner runner.json
```

The CLI exits 0 for an accepted handoff and 2 for a rejected one. It has no
production path defaults and writes no files.
