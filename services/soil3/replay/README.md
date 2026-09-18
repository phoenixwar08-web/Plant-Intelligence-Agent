# soil3 historical Replay Sample V1

This module reconstructs a read-only `state.v1` from facts whose timestamps are
at or before an inclusive historical cutoff `T`. It also emits a separate data
quality report for continuity gaps, missing fields, invalid timestamps, source
ordering, duplicates, and watering-to-sensor relationships.

The replay sample never includes future fact values or future record counts.
The quality report may state how many source rows were excluded for occurring
after `T`, but it does not copy those future values into the sample.

Example:

```bash
python -m services.soil3.replay.service \
  --at 2026-09-16T12:00:00Z \
  --sensor-csv /path/to/read-only/sensor_log.csv \
  --watering-json /path/to/read-only/irrigation_trials.json \
  --sample-output /tmp/replay_sample.json \
  --quality-output /tmp/replay_quality.json
```

State and parameter history are optional JSONL inputs. When they are absent,
the generated State preserves those facts as unknown and the report lists the
corresponding limitation. The tool never writes its source files, retrains a
model, imports Phase3, or exposes any device-control path.
