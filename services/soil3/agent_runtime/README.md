# Soil3 proposal-only runtime

This runtime reads existing soil3 health facts, writes a frozen `state.v1`
snapshot, creates an explicitly labelled `offline_fixture` or
`qwen_dashscope` `strategy.v1`, and records its Gate result plus a closed
Episode. Both modes are proposal-only: the runtime does not create an actuator
command, call Phase3, or publish MQTT.

The first deployment intentionally sets `exploration_requested` to `false`.
Gate denial is an expected safety result: Runner is skipped, and the closed
Episode lists absent executed actions, feedback, and outcome as missing facts.
If Gate admits the fixture, Runner performs only its persisted dry-run logic;
its record declares both physical actions and Phase3 calls false.

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

After inspecting the generated `state`, `strategy`, `gate`, `runs`, and
`episodes` records below `/root/water/runtime/instances/soil3/agent_chain`,
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
