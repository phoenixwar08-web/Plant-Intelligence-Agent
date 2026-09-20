# Soil3 proposal-only runtime

This runtime reads the existing soil3 health facts, writes a frozen `state.v1`
snapshot, creates an explicitly labelled `offline_fixture` `strategy.v1`, and
records its Gate result plus a closed Episode. It does not connect to a real
provider, create an actuator command, call Phase3, or publish MQTT.

The first deployment intentionally sets `exploration_requested` to `false`.
Gate denial is an expected safety result: Runner is skipped, and the closed
Episode lists absent executed actions, feedback, and outcome as missing facts.
If Gate admits the fixture, Runner performs only its persisted dry-run logic;
its record declares both physical actions and Phase3 calls false.

## Install and validate

The release directory must contain the merged `main` revision and a `REVISION`
file before the units are installed. Copy the example configuration to the
dedicated runtime directory with mode 600; it contains no secret.

```bash
install -d -m 755 /root/water/runtime/instances/soil3/agent_chain/config
install -m 600 config/soil3_agent_runtime.example.json \
  /root/water/runtime/instances/soil3/agent_chain/config/runtime.json
install -m 644 deploy/systemd/plant-agent-soil3-state.service \
  deploy/systemd/plant-agent-soil3-state.timer \
  deploy/systemd/plant-agent-soil3-pipeline.service \
  deploy/systemd/plant-agent-soil3-pipeline.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/plant-agent-soil3-state.service \
  /etc/systemd/system/plant-agent-soil3-state.timer \
  /etc/systemd/system/plant-agent-soil3-pipeline.service \
  /etc/systemd/system/plant-agent-soil3-pipeline.timer
systemctl daemon-reload
systemctl start plant-agent-soil3-pipeline.service
systemctl status --no-pager plant-agent-soil3-state.service plant-agent-soil3-pipeline.service
```

After inspecting the generated `state`, `strategy`, `gate`, `runs`, and
`episodes` records below `/root/water/runtime/instances/soil3/agent_chain`,
enable the independently reversible schedules:

```bash
systemctl enable --now plant-agent-soil3-state.timer plant-agent-soil3-pipeline.timer
```

## Disable and rollback

```bash
systemctl disable --now plant-agent-soil3-pipeline.timer plant-agent-soil3-state.timer
rm -f /etc/systemd/system/plant-agent-soil3-state.service /etc/systemd/system/plant-agent-soil3-state.timer \
  /etc/systemd/system/plant-agent-soil3-pipeline.service /etc/systemd/system/plant-agent-soil3-pipeline.timer
systemctl daemon-reload
```

Delete a release or runtime directory only after both timers are disabled and
the target has been verified as the `agent_chain` directory, never a Phase3
runtime/data/log directory. Preserve generated records during acceptance for
traceability.
