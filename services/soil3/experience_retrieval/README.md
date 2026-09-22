# soil3 Experience Retrieval V1

This module reads closed `episode.v1` files and independently ranks the most
similar explicitly successful and failed cases for a current `state.v1`.

Similarity is deterministic and explainable. The response includes every
available feature's current value, historical value, absolute distance,
normalized similarity and fixed weight. It covers soil humidity, 1/3/6-hour
humidity trends, air humidity and temperature, last watering duration and
recency, Phase3 safety thresholds and active safety flags. Missing dimensions
reduce `evidence_coverage`; they are never filled in. A case below the minimum
coverage is not returned as usable.

`episode.v1` intentionally has no unified reward. Retrieval therefore labels
an outcome only when one of `experience_result`, `result`, `status`,
`classification`, or `recovery` contains an explicit supported label. The
success labels are `success`, `succeeded`, `effective`, `good`, and
`recovered`; failure labels are `failure`, `failed`, `ineffective`, `poor`,
and `adverse`. Missing, unknown, or conflicting labels stay unclassified.
Measurements, free text, actions, and Gate decisions are never guessed into an
outcome.

An empty class is explicit through `availability`, `empty_reasons`, and the
empty result list. `skipped_history` explains why records were unavailable.
The module uses no vector database and has no strategy, Phase3, actuator,
MQTT, pump, Episode-write, Feedback, or Bridge dependency.

```bash
python -m services.soil3.experience_retrieval.service \
  --episode-dir /path/to/episodes --state /path/to/state.json \
  --limit-per-class 3
```
