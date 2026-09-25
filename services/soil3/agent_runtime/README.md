# Soil3 proposal-only runtime

This runtime reads existing soil3 health facts and runs one complete Shadow
decision chain:

```text
State → explicit Vision/Experience availability → Strategy → Validator
      → Gate v2 → dry-run Runner → verification-only Bridge → open Episode
```

Every produced artifact is associated through one Store-generated `trace.v1`
identifier. Vision and Experience each have an explicit configuration switch.
When disabled they remain `not_requested`; a requested call that cannot produce
validated facts is `unavailable` with a null reference. Available Vision facts
are linked through the public `vision_run.v1` manifest, while successful
Experience Retrieval output is persisted beneath the agent runtime. The same
available facts are passed to Cloud Strategy as read-only supporting context;
no missing value is filled in. Model token usage is recorded only when the
provider supplied `total_tokens`; cost remains null because the current
provider envelope does not supply it, and latency comes from the runtime
monotonic clock.

Runner and Bridge are reached only after a content-bound, non-deny `gate.v2`.
Bridge verifies the exact State, Strategy, Gate and terminal dry-run Runner
record, then writes a verification response. Even an accepted response does
not call Phase3. The Episode remains open for delayed `feedback.v1` and
Outcome. Both provider modes are Shadow-only: the runtime does not create an
actuator command, call Phase3, or publish MQTT.

The first deployment intentionally sets `exploration_requested` to `false`.
Gate denial or an invalid Gate binding is an expected safety result: Runner
and Bridge are skipped, while the Episode stays open with absent executed
actions until the feedback lifecycle finalizes it.
If Gate admits the fixture, Runner performs only its persisted dry-run logic;
Bridge performs verification only, and every Runner, Bridge, Trace and runtime
record declares both physical actions and Phase3 calls false.
Trace execution remains `phase3_called=false` and
`physical_actions_performed=false` for every optional-module state.

Every successful pipeline record states `episode_status: open` and
`feedback_status: pending`. Feedback windows may be attached incrementally;
the final intended window attaches the aggregated Outcome and closes the
Episode through `EpisodeStore.close()`. The runtime never reopens or mutates a
closed Episode.

`offline_fixture` is the release-default test and explicit rollback mode. It
is never a silent replacement for Qwen. A Qwen request/validation failure
writes a failed non-executing run record and does not call Gate, Runner, or
Episode.

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

The state and pipeline units keep `PrivateTmp=true`.  They optionally bind only
the openGauss local socket `/tmp/.s.PGSQL.7654` read-only, so the existing
`runuser -u opengauss` SELECT adapter can read canonical telemetry without
exposing the rest of host `/tmp`.  If that socket is unavailable, the optional
bind does not make the unit fail open: the adapter query stays unavailable and
soil humidity/freshness remain missing.  It does not use CSV fallback or write
to openGauss.

After inspecting the generated `state`, `strategy`, `gate`, `runner`, `bridge`,
`traces`, `runs`, and `episodes` records below
`/root/water/runtime/instances/soil3/agent_chain`,
enable the independently reversible schedules:

```bash
systemctl enable --now plant-agent-soil3-state.timer plant-agent-soil3-pipeline.timer
```

## Qwen shadow-mode smoke after Owner merge

Do not perform this procedure from an unmerged PR. Install the Owner-merged
`main` release first, then stop only the new pipeline timer. Do not restart or
modify Phase3, MQTT, ActuatorLayer, or the production telemetry writer.

~~~bash
systemctl stop plant-agent-soil3-pipeline.timer
systemctl is-active phase3_soil3.service mqtt_direct_gauss.service mqtt_logger3.service
install -d -m 700 /root/water/runtime/instances/soil3/agent_chain/secrets
install -m 600 /dev/null /root/water/runtime/instances/soil3/agent_chain/secrets/qwen.env
~~~

The Owner places the configured API-key environment variable and its secret
value directly in that mode-0600 file, outside Git and shell history. Do not
put the value in `runtime.json`, a command line, journal command, audit, or
Episode.

Change the dedicated runtime configuration to the explicit Qwen mode. The
only accepted provider settings are shown below; the API key itself is not a
JSON value.

~~~json
{
  "provider_mode": "qwen_dashscope",
  "provider": {
    "base_url": "https://ws-d5yw23tz0yzwob1l.cn-beijing.maas.aliyuncs.com/api/v1",
    "model": "qwen3.8-Flash",
    "api_key_env": "QWEN_DASHSCOPE_API_KEY",
    "timeout_seconds": 30,
    "max_retries": 1,
    "temperature": 0,
    "max_tokens": 1200,
    "json_response_format": true
  }
}
~~~

Run one isolated proposal-only cycle and inspect its artifacts before
re-enabling the timer:

~~~bash
systemctl start plant-agent-soil3-pipeline.service
systemctl status --no-pager plant-agent-soil3-pipeline.service
~~~

Acceptance requires a `strategy.v1` and audit that identify
`qwen_dashscope` and `qwen3.8-Flash`, a `state.v1` that contains soil humidity
and receive-time freshness only when the read-only openGauss query returned a
valid row, Gate's actual decision, and a traceable Episode when validation
succeeds. Gate `deny` must skip Runner. If Gate admits, Runner remains
dry-run and its record must report both physical actions and Phase3 calls as
false.

Only after that smoke passes may the Owner re-enable the five-minute shadow
timer:

~~~bash
systemctl enable --now plant-agent-soil3-pipeline.timer
~~~

On a Qwen transport, schema, or validation failure, leave the pipeline timer
disabled or explicitly restore `offline_fixture`; never allow an automatic
fallback. Both paths remain non-physical.

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
