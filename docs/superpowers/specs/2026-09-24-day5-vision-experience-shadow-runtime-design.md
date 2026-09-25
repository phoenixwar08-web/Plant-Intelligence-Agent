# Day 5 Vision and Experience Shadow Runtime Integration Design

## Goal

Integrate the existing public Vision V1 and Experience Retrieval V1 modules into
the soil3 Shadow runtime so that their validated facts are persisted, linked by
`trace.v1`, and supplied to Cloud Strategy before validation and Gate admission.
The resulting path is:

```text
State → Vision / Experience → Strategy → Validator → Gate v2
      → Runner dry-run → Bridge verification-only → Episode
```

The integration remains proposal-only. It does not call Phase3, MQTT,
ActuatorLayer, `manual_water`, or a physical pump.

## Current state

The runtime currently writes both Trace associations as
`{"availability":"not_requested","ref":null}` and invokes neither module.
`ExperienceRetriever.retrieve(state)` already returns a complete structured
result but does not persist it. Vision persists successful `vision.v1` zone
records internally and returns validated records in `VisionRunResult`, but it
does not expose one stable artifact representing the whole capture run. Cloud
Strategy currently sends only a reduced State projection to the provider.

## Scope and non-goals

This change may modify the agent runtime, the Vision public result boundary,
Cloud Strategy model-input projection and prompt, configuration examples,
documentation, and tests.

It must not modify:

- `state.v1`, `vision.v1`, `experience_retrieval.v1`, `trace.v1`,
  `strategy.v1`, or `gate.v2` semantics;
- Strategy Validator limits or Gate admission rules;
- Runner, Bridge, Phase3, MQTT, ActuatorLayer, or physical execution;
- production data, runtime data, secrets, or deployed openEuler services.

An Episode continues to contain its existing State, Strategy, Gate, action,
Feedback, and Outcome fields. Vision and Experience are associated through
Trace and consumed through the Strategy model input; Episode V1 is not widened.

## Considered approaches

### Selected: Vision-owned run manifest

Vision creates and returns a stable manifest artifact for a run containing at
least one valid zone observation. The runtime consumes the public artifact
reference and never derives Vision's internal record paths. This keeps
ownership of Vision persistence in the Vision module and leaves `trace.v1`
unchanged.

### Rejected: runtime-owned copy of Vision results

The runtime could serialize `VisionRunResult` itself. That would duplicate
Vision facts and make the runtime the owner of a Vision artifact format. It
would also allow Vision persistence and Trace references to drift.

### Rejected: multiple Vision refs in trace.v1

Changing Trace to carry one reference per plant zone would widen an already
implemented protocol and is unnecessary. One Vision-owned manifest provides a
single stable association while retaining per-zone references.

## Runtime configuration

`RuntimeConfig` gains two exact objects:

```json
{
  "vision": {"enabled": false},
  "experience": {"enabled": false, "limit_per_class": 3}
}
```

Rules:

- `enabled` must be a boolean.
- `limit_per_class` must be an integer from 1 through 20.
- Disabled modules are not called and are recorded as `not_requested`.
- Vision continues to use its existing environment-backed public configuration
  for camera, provider, zones, and data root. Secrets are not copied into the
  runtime JSON.
- Experience reads the runtime's existing Episode directory and never writes
  Episode records.
- The checked-in example keeps both modules disabled, preserving an offline,
  nonsecret default.

## Vision public artifact boundary

Vision adds a small public immutable artifact type:

```python
@dataclass(frozen=True)
class VisionArtifactRef:
    schema_version: str
    record_id: str
    path: str
    sha256: str
```

`CaptureOutcome` gains an optional zone-record artifact reference and
`VisionRunResult` gains an optional manifest artifact reference. Defaults are
`None` so existing callers that construct these dataclasses remain compatible.

For each successful zone, `VisionService` returns the reference produced while
persisting the already validated `vision.v1` record. When at least one zone is
successful, it atomically persists a `vision_run.v1` manifest under the Vision
data root. The manifest contains:

```json
{
  "schema_version": "vision_run.v1",
  "run_id": "UUID",
  "device_code": "soil3",
  "created_at": "RFC3339 UTC",
  "status": "success, image_unusable, or partial",
  "frame_id": "UUID",
  "outcomes": [
    {
      "zone_id": "plant_zone_1",
      "status": "success",
      "artifact_ref": {
        "schema_version": "vision.v1",
        "record_id": "image UUID",
        "path": "absolute persisted JSON path",
        "sha256": "64 lowercase hex characters"
      },
      "error_code": null,
      "http_status": null,
      "provider_error_code": null
    }
  ]
}
```

Every configured zone appears in `outcomes`. Failed zones retain their status
and diagnostics with a null artifact reference; zones that produced a validated
observation retain its public artifact reference. The returned manifest
reference has `schema_version=vision_run.v1` and is the
single artifact recorded in Trace. A run with no valid observation returns no
manifest reference. Existing per-zone failure records remain Vision-owned.

## Availability and failure semantics

Each optional module produces a runtime association and a Strategy context.

| Condition | Trace availability | Trace ref | Strategy facts |
| --- | --- | --- | --- |
| disabled | `not_requested` | `null` | `null` |
| call failed or Vision produced no valid observation | `unavailable` | `null` | `null` |
| successful result | `available` | persisted artifact ref | validated facts |

Vision configuration errors and normal capture/analysis failures are converted
to `unavailable`; no record is invented. Unexpected programming errors are not
silently swallowed. Experience request validation failures are converted to
`unavailable`. An Experience result with zero usable historical cases remains
`available`: its existing `availability`, empty lists, and `empty_reasons`
truthfully describe the empty result.

Optional-context failure does not grant permission and does not bypass any
downstream control. Strategy receives the explicit unavailable state and may
only propose; Validator and Gate continue to decide independently.

## Persisted Experience result

When enabled, the runtime calls the existing public interface:

```python
ExperienceRetriever(config.episode_dir).retrieve(
    state,
    limit_per_class=config.experience_limit_per_class,
)
```

Successful results are atomically stored at:

```text
<runtime_root>/experience/<trace_id>.json
```

Trace references that artifact with
`schema_version=experience_retrieval.v1`, its file hash, and
`record_id=<trace_id>`. No retrieval result is copied into Trace.

## Cloud Strategy input

`run_chain()` gains two required-by-runtime but optional-for-other-callers
keyword parameters, `vision_context` and `experience_context`. Omitting them
preserves current callers by representing each context as `not_requested`.

The existing projected State fields remain at the model-input top level so
Strategy binding fields do not change. Two additional top-level objects are
added:

```json
{
  "vision": {
    "availability": "available | unavailable | not_requested",
    "facts": null
  },
  "experience": {
    "availability": "available | unavailable | not_requested",
    "facts": null
  }
}
```

For available Vision, `facts` is a list of validated per-zone observations
projected to decision-relevant fields: plant-zone identity, capture time,
image quality, target detection, visual severity fields, leaf spread,
wilting, overall visual state, change versus previous observation, and
confidence. Image bytes, image paths, hashes, provider diagnostics, and model
credentials are excluded.

For available Experience, `facts` is the existing retrieval result. It already
contains bounded successful/failed cases, similarity evidence, explicit empty
reasons, and no actuator capability.

The Cloud Strategy prompt explains both contexts, requires the model to treat
`unavailable` and `not_requested` as absence, and forbids inferring missing
facts. The output contract and `strategy-prompt.v2` identifier remain unchanged
because `strategy.v1` fields and validation rules do not change.

The complete model input remains in the existing Cloud Strategy audit record,
providing evidence that Strategy actually received the contexts.

## Runtime ordering

One pipeline run performs these steps:

1. Create Trace and persist State.
2. If enabled, call Vision through its public one-shot API.
3. If enabled, call Experience Retrieval with the exact persisted State.
4. Persist successful optional-module results.
5. Set State, Vision, and Experience Trace decision associations once.
6. Pass the two explicit contexts into `run_chain()`.
7. Continue through the existing Strategy validation, Gate v2 admission,
   dry-run Runner, verification-only Bridge, and open Episode flow unchanged.

The runtime never loads a Vision record by reconstructing its internal path.
It uses only the public `VisionRunResult`, validated observation values, and
manifest reference returned by Vision.

## Testing

Tests cover both modules independently and together:

- disabled: public services are not called; Trace and Strategy audit show
  `not_requested` with null facts and refs;
- available Vision: public result contains a real manifest reference, Trace
  hash/path match it, and Strategy audit receives projected validated facts;
- unavailable Vision: configuration/capture failure produces
  `unavailable`, null ref, null facts, and the remaining Shadow chain stays
  non-executing;
- available Experience, including empty history: result is persisted, Trace
  references it, and Strategy audit receives the exact retrieval result;
- unavailable Experience: documented retrieval failure produces
  `unavailable`, null ref, and null Strategy facts;
- combined available case: both contexts are present before Strategy;
- existing Strategy Validator, Gate v2, Runner dry-run, Bridge
  verification-only, Trace, Episode, and Day 5 fault suites remain green;
- AST/control-boundary checks continue to prove no Phase3, MQTT,
  ActuatorLayer, or physical execution dependency was added.

## Documentation and deployment boundary

The runtime and Vision READMEs, System Architecture, example configuration,
and protocol boundary documentation are updated only to describe the new
implemented data flow. No openEuler deployment, camera/provider smoke test,
service enablement, or Day-level production validation occurs in this PR.
