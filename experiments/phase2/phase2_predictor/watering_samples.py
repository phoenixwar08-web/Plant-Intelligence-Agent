from bisect import bisect_left


def _nearest_index(timestamps, target, start, tolerance):
    """Return the first observation at or after a target, never an early label."""
    index = bisect_left(timestamps, target, lo=start)
    if index >= len(timestamps):
        return None
    return index if 0.0 <= timestamps[index] - target <= tolerance else None


def _is_watering_row(metadata):
    return metadata.get("watered", 0) > 0 or metadata.get("water_seconds", 0) > 0


def _append_segment(segments, segment):
    if segment and segment["has_watering"]:
        segments.append(segment)


def _has_watering_between(rows, start, end):
    for index in range(start, end + 1):
        if _is_watering_row(rows[index][3]):
            return True
    return False


def _next_watering_index(rows, start):
    for index in range(start, len(rows)):
        if _is_watering_row(rows[index][3]):
            return index
    return None


def _watering_segments(rows, config):
    segments = []
    current = None
    maximum_gap = float(config["phase_detection"]["maximum_watering_merge_gap_seconds"])
    for index, row in enumerate(rows):
        metadata = row[3]
        if metadata.get("learned_phase") == "NATURAL":
            if current:
                current["end"] = index - 1
                current["complete"] = True
                _append_segment(segments, current)
                current = None
            continue
        segment_id = metadata.get("segment_id")
        if current and row[0] - rows[current["end"]][0] > maximum_gap:
            current["end"] = index - 1
            _append_segment(segments, current)
            current = None
        if current is None or current["segment_id"] != segment_id:
            if current:
                current["end"] = index - 1
                _append_segment(segments, current)
            current = {
                "segment_id": segment_id, "start": index, "end": index,
                "water_seconds": 0.0, "has_watering": False,
                "training_eligible": True, "training_label": "normal_response",
                "complete": False, "peak_complete": False,
            }
        current["end"] = index
        current["water_seconds"] += float(metadata.get("water_seconds", 0.0))
        current["has_watering"] = current["has_watering"] or _is_watering_row(metadata)
        current["peak_complete"] = (
            current["peak_complete"]
            or bool(metadata.get("segment_peak_complete", False))
        )
        if _is_watering_row(metadata):
            current["training_eligible"] = current["training_eligible"] and metadata.get(
                "training_eligible", True
            )
            current["training_label"] = metadata.get("training_label", current["training_label"])
    if current:
        _append_segment(segments, current)
    return segments

def build_watering_samples(rows, config):
    """One sample per watering event, labeled by its following 12-hour response.

    The watering model must learn continuous dose response: if history contains
    4s, 5s, 6s, etc., those seconds are part of the input sequence and the
    future hourly humidity labels teach the model what that dose did. Labels
    after a later watering are masked out so one event is not trained on another
    event's effect.
    """
    timestamps = [row[0] for row in rows]
    sequence_length = int(config["model"]["sequence_length"])
    tolerance = float(config["offline_training"]["target_tolerance_seconds"])
    samples = []
    for segment in _watering_segments(rows, config):
        if not segment.get("training_eligible", True):
            continue
        event = segment["start"]
        if event < sequence_length - 1:
            continue
        target_indices = []
        trajectory = []
        mask = []
        for hour in range(1, 13):
            target = _nearest_index(timestamps, rows[event][0] + hour * 3600, event + 1, tolerance)
            if target is None:
                target_indices.append(None)
                trajectory.append(rows[event][2])
                mask.append(0.0)
                continue
            target_indices.append(target)
            trajectory.append(rows[target][2])
            mask.append(
                0.0 if _has_watering_between(rows, event + 1, target) else 1.0
            )
        if not any(mask):
            continue
        sequence = [row[1] for row in rows[event - sequence_length + 1:event + 1]]
        next_watering = _next_watering_index(rows, event + 1)
        last_valid_target = max(
            (
                target for target, valid in zip(target_indices, mask)
                if valid and target is not None
            ),
            default=segment["end"],
        )
        observation_end = last_valid_target
        if next_watering is not None:
            observation_end = min(observation_end, next_watering - 1)
        observation_end = max(event, observation_end)
        peak = max(row[2] for row in rows[event:observation_end + 1])
        peak_index = max(
            range(event, observation_end + 1), key=lambda index: rows[index][2]
        )
        metadata = {
            **rows[event][3], "event_timestamp": rows[event][0],
            "humidity": rows[event][3].get(
                "training_source_humidity", rows[event][3]["humidity"]
            ),
            "target_timestamp": rows[event][0] + 43200,
            "segment_id": segment["segment_id"], "water_seconds": segment["water_seconds"],
            "training_eligible": segment.get("training_eligible", True),
            "training_label": segment.get("training_label", "normal_response"),
            "segment_complete": bool(segment.get("complete", False)),
            "mask": mask, "observed_response_hours": int(sum(mask)),
            "minutes_to_peak": min(720.0, max(0.0, (rows[peak_index][0] - rows[event][0]) / 60.0)),
            "minutes_to_natural": min(
                720.0, max(0.0, (rows[segment["end"]][0] - rows[event][0]) / 60.0)
            ),
            "intervened": any(value == 0.0 for value in mask),
        }
        samples.append((sequence, trajectory, peak, metadata))
    return samples


def build_dose_response_samples(rows, config):
    samples = []
    sequence_length = int(config["model"]["sequence_length"])
    for segment in _watering_segments(rows, config):
        if not segment.get("training_eligible", True):
            continue
        if not (
            segment.get("complete", False)
            or segment.get("peak_complete", False)
        ):
            continue
        if segment["water_seconds"] <= 0:
            continue
        event = segment["start"]
        if event < sequence_length - 1:
            continue
        sequence = [row[1] for row in rows[event - sequence_length + 1:event + 1]]
        peak = max(row[2] for row in rows[event:segment["end"] + 1])
        peak_index = max(
            range(event, segment["end"] + 1), key=lambda index: rows[index][2]
        )
        samples.append((sequence, peak, {
            **rows[event][3], "event_timestamp": rows[event][0],
            "humidity": rows[event][3].get(
                "training_source_humidity", rows[event][3]["humidity"]
            ),
            "segment_id": segment["segment_id"], "water_seconds": segment["water_seconds"],
            "training_eligible": segment.get("training_eligible", True),
            "training_label": segment.get("training_label", "normal_response"),
            "segment_complete": True,
            "segment_closed_by_next_watering": bool(
                segment.get("peak_complete", False)
                and not segment.get("complete", False)
            ),
            "minutes_to_peak": min(720.0, max(0.0, (rows[peak_index][0] - rows[event][0]) / 60.0)),
            "minutes_to_natural": min(
                720.0, max(0.0, (rows[segment["end"]][0] - rows[event][0]) / 60.0)
            ),
        }))
    return samples
