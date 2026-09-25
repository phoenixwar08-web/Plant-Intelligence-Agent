# Soil3 Vision V1

Vision records appearance facts for configured plant zones. It does not propose
care, call Phase3, publish MQTT, or control an actuator.

A successful zone observation persists one validated `vision.v1` record. A
capture cycle with at least one such record also persists one public
`vision_run.v1` manifest whose `outcomes` preserve every zone status and include
an artifact reference whenever that zone produced a record.
`VisionRunResult.manifest_ref` contains the manifest path and the
SHA-256 of the exact persisted bytes, so callers do not reconstruct Vision's
private directory layout.

Capture failure or a cycle with no validated observation returns no manifest
reference. Consumers must represent that result as unavailable rather than
inventing visual facts. Vision remains non-executing; downstream Trace records
continue to declare `phase3_called=false` and
`physical_actions_performed=false`.
