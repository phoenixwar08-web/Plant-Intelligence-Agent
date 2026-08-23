import copy
import logging
import os
import random
import tempfile

import torch
import torch.nn as nn
import torch.optim as optim

from .device import resolve_torch_device


class WateringResponseTransformer(nn.Module):
    """Predicts 12 hourly humidities and the post-watering peak humidity."""

    def __init__(self, input_dim=9, seq_len=12, hidden_dim=32):
        super().__init__()
        self.feature_proj = nn.Linear(input_dim, hidden_dim)
        self.position = nn.Parameter(torch.zeros(1, seq_len, hidden_dim))
        layer = nn.TransformerEncoderLayer(
            d_model=hidden_dim, nhead=4, dim_feedforward=hidden_dim * 2,
            dropout=0.1, batch_first=True, norm_first=False,
        )
        self.encoder = nn.TransformerEncoder(layer, num_layers=1)
        self.output_norm = nn.LayerNorm(hidden_dim)
        self.trajectory_head = nn.Linear(hidden_dim, 12)
        self.peak_head = nn.Linear(hidden_dim, 1)
        self.time_to_peak_head = nn.Linear(hidden_dim, 1)
        self.time_to_natural_head = nn.Linear(hidden_dim, 1)

    def forward(self, values):
        values = self.feature_proj(values) + self.position[:, :values.shape[1], :]
        encoded = self.output_norm(torch.mean(self.encoder(values), dim=1))
        return (
            torch.sigmoid(self.trajectory_head(encoded)),
            torch.sigmoid(self.peak_head(encoded)).squeeze(1),
            torch.sigmoid(self.time_to_peak_head(encoded)).squeeze(1),
            torch.sigmoid(self.time_to_natural_head(encoded)).squeeze(1),
        )


class WateringModelRuntime:
    def __init__(self, config, logger=None, weight_path_key="shadow_weights"):
        self.logger = logger or logging.getLogger(__name__)
        self.weight_path_key = weight_path_key
        self.config = config
        self.path = config["paths"][weight_path_key]
        self.is_natural = self.weight_path_key == "natural_weights"
        model = config["model"]
        self.requested_device = model.get("device", "cpu")
        self.device, self.device_status = resolve_torch_device(
            self.requested_device, self.logger, model.get("npu_selftest_timeout_seconds", 12)
        )
        torch.set_num_threads(int(model.get("num_threads", 1)))
        self.model = WateringResponseTransformer(
            model["input_dim"], model["sequence_length"], model["hidden_dim"]
        ).to(self.device)
        self.status = "fresh"
        self.weights_mtime = None
        if os.path.exists(self.path):
            try:
                try:
                    state = torch.load(self.path, map_location=self.device, weights_only=True)
                except TypeError:
                    state = torch.load(self.path, map_location=self.device)
                self.model.load_state_dict(state)
                self.status = "loaded"
                self.weights_mtime = os.path.getmtime(self.path)
            except Exception:
                self.logger.warning("Existing weights are incompatible; starting fresh", exc_info=True)
        self.optimizer = optim.Adam(self.model.parameters(), lr=float(config["training"]["learning_rate"]))
        self.loss_fn = nn.SmoothL1Loss(beta=0.05)
        self.model.eval()

    def update_config(self, config):
        """Apply reloadable settings while preserving learned model state."""
        self.config = config
        self.path = config["paths"][self.weight_path_key]
        for group in self.optimizer.param_groups:
            group["lr"] = float(config["training"]["learning_rate"])

    def reload_if_changed(self):
        """Hot-reload promoted or shadow weights without restarting the service."""
        if not os.path.exists(self.path):
            return False
        mtime = os.path.getmtime(self.path)
        if self.weights_mtime is not None and mtime == self.weights_mtime:
            return False
        return self.reload_weights()

    def _smooth_watering_trajectory(self, values, current, predicted_peak):
        if not values:
            return values
        maximum_jump = float(self.config.get("prediction", {}).get("maximum_step_jump", 3.0))
        peak_index = max(range(len(values)), key=lambda index: values[index])
        smoothed = []
        previous = current
        for index, value in enumerate(values):
            value = min(float(predicted_peak), value)
            if index <= peak_index:
                value = max(previous, value)
            else:
                value = min(previous, value)
            value = max(previous - maximum_jump, min(previous + maximum_jump, value))
            smoothed.append(value)
            previous = value
        return smoothed

    def reload_weights(self, path=None):
        source = path or self.path
        if not os.path.exists(source):
            return False
        try:
            try:
                state = torch.load(source, map_location=self.device, weights_only=True)
            except TypeError:
                state = torch.load(source, map_location=self.device)
            self.model.load_state_dict(state)
            self.model.eval()
            self.status = "loaded"
            self.weights_mtime = os.path.getmtime(source)
            return True
        except Exception:
            self.logger.exception("Failed to restore accepted weights from %s", source)
            return False

    def predict(self, sequence):
        tensor = torch.tensor([sequence], dtype=torch.float32, device=self.device)
        with torch.inference_mode():
            trajectory, peak, time_to_peak, time_to_natural = self.model(tensor)
        values = [float(value * 100.0) for value in trajectory[0]]
        current = float(sequence[-1][2] * 100.0)
        predicted_peak = max(current, float(peak.item() * 100.0))
        constrained = []
        start = current
        for value in values:
            value = max(current - 5.0, min(current + (0.5 if self.is_natural else 5.0), value))
            if not self.is_natural:
                value = min(value, predicted_peak)
            constrained.append(value)
            current = value
        if not self.is_natural:
            constrained = self._smooth_watering_trajectory(constrained, start, predicted_peak)
        else:
            predicted_peak = max([start] + constrained)
        return constrained, predicted_peak

    def predict_details(self, sequence):
        trajectory, peak = self.predict(sequence)
        tensor = torch.tensor([sequence], dtype=torch.float32, device=self.device)
        with torch.inference_mode():
            _trajectory, _peak, time_to_peak, time_to_natural = self.model(tensor)
        return {
            "trajectory": trajectory,
            "peak": peak,
            "minutes_to_peak": float(time_to_peak.item() * 720.0),
            "minutes_to_natural": float(time_to_natural.item() * 720.0),
        }

    def _trajectory_loss(self, predictions, targets, masks=None, focus_h12=False):
        point_losses = nn.functional.smooth_l1_loss(
            predictions, targets, beta=0.05, reduction="none"
        )
        if masks is None:
            masks = torch.ones_like(point_losses)
        loss_weights = self.config["loss_weights"]
        weights = torch.ones_like(point_losses)
        if focus_h12 and not self.is_natural:
            weights[:, -1] = float(loss_weights["trajectory_h12_point"])
        weighted_masks = masks * weights
        base = torch.sum(point_losses * weighted_masks) / torch.clamp(torch.sum(weighted_masks), min=1.0)
        differences = predictions[:, 1:] - predictions[:, :-1]
        smoothness = torch.mean(torch.abs(differences))
        natural_rise = torch.mean(torch.relu(differences)) if self.is_natural else predictions.new_tensor(0.0)
        return base + float(loss_weights["smoothness"]) * smoothness + float(loss_weights["natural_rise"]) * natural_rise

    def train_batch(self, samples, gradient_clip):
        if not samples:
            return None
        sequences = torch.tensor([sample[0] for sample in samples], dtype=torch.float32, device=self.device)
        trajectories = torch.tensor(
            [[value / 100.0 for value in sample[1]] for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        peaks = torch.tensor([sample[2] / 100.0 for sample in samples], dtype=torch.float32, device=self.device)
        masks = torch.tensor(
            [sample[3].get("mask", [1.0] * 12) for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        self.model.train()
        self.optimizer.zero_grad()
        predicted_trajectories, predicted_peaks, predicted_peak_times, predicted_natural_times = self.model(sequences)
        trajectory_loss = self._trajectory_loss(
            predicted_trajectories, trajectories, masks, focus_h12=True
        )
        peak_loss = self.loss_fn(predicted_peaks, peaks)
        h12_mask = masks[:, -1] > 0.0
        if torch.any(h12_mask):
            h12_loss = self.loss_fn(predicted_trajectories[h12_mask, -1], trajectories[h12_mask, -1])
        else:
            h12_loss = predicted_trajectories.new_tensor(0.0)
        consistency = self.loss_fn(predicted_peaks, torch.max(predicted_trajectories, dim=1).values)
        peak_times = torch.tensor(
            [sample[3].get("minutes_to_peak", 0.0) / 720.0 for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        natural_times = torch.tensor(
            [sample[3].get("minutes_to_natural", 720.0) / 720.0 for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        time_loss = self.loss_fn(predicted_peak_times, peak_times) + self.loss_fn(
            predicted_natural_times, natural_times
        )
        loss_weights = self.config["loss_weights"]
        if self.is_natural:
            loss = trajectory_loss + float(loss_weights["consistency"]) * consistency + float(loss_weights["time"]) * time_loss
        else:
            loss = float(loss_weights["trajectory"]) * trajectory_loss + float(loss_weights["peak"]) * peak_loss + float(loss_weights["h12"]) * h12_loss + float(loss_weights["consistency"]) * consistency + float(loss_weights["time"]) * time_loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def train_trajectory_batch(self, samples, gradient_clip):
        if not samples:
            return None
        sequences = torch.tensor([sample[0] for sample in samples], dtype=torch.float32, device=self.device)
        targets = torch.tensor(
            [[value / 100.0 for value in sample[1]] for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        self.model.train()
        self.optimizer.zero_grad()
        predictions, _peak, _peak_time, _natural_time = self.model(sequences)
        loss = self._trajectory_loss(predictions, targets, focus_h12=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def train_masked_trajectory_batch(self, samples, gradient_clip):
        """Train only newly available hours; each sample is (sequence, targets, mask)."""
        if not samples:
            return None
        sequences = torch.tensor([sample[0] for sample in samples], dtype=torch.float32, device=self.device)
        targets = torch.tensor(
            [[value / 100.0 for value in sample[1]] for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        masks = torch.tensor([sample[2] for sample in samples], dtype=torch.float32, device=self.device)
        self.model.train()
        self.optimizer.zero_grad()
        predictions, _peak, _peak_time, _natural_time = self.model(sequences)
        loss = self._trajectory_loss(predictions, targets, masks, focus_h12=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def train_peak_batch(self, samples, gradient_clip):
        if not samples:
            return None
        sequences = torch.tensor([sample[0] for sample in samples], dtype=torch.float32, device=self.device)
        targets = torch.tensor([sample[1] / 100.0 for sample in samples], dtype=torch.float32, device=self.device)
        self.model.train()
        self.optimizer.zero_grad()
        _trajectory, predictions, _peak_time, _natural_time = self.model(sequences)
        loss = self.loss_fn(predictions, targets)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def fit_peak_head(
        self, train_samples, validation_samples, epochs, patience,
        learning_rate, gradient_clip, batch_size,
    ):
        """Recalibrate only peak_head; preserve encoder and trajectory behavior."""
        if not train_samples:
            return None
        validation_samples = validation_samples or train_samples
        original_flags = {
            parameter: parameter.requires_grad for parameter in self.model.parameters()
        }
        for parameter in self.model.parameters():
            parameter.requires_grad = False
        for parameter in self.model.peak_head.parameters():
            parameter.requires_grad = True
        optimizer = optim.Adam(
            self.model.peak_head.parameters(), lr=float(learning_rate)
        )
        best_state = copy.deepcopy(self.model.peak_head.state_dict())
        self.model.eval()
        best_mae = self.evaluate_peaks(validation_samples)
        best_mae = float("inf") if best_mae is None else float(best_mae)
        last_loss = None
        stale_epochs = 0
        completed_epochs = 0
        rng = random.Random(20260622)
        try:
            for epoch in range(int(epochs)):
                shuffled = list(train_samples)
                rng.shuffle(shuffled)
                epoch_losses = []
                # Keep the frozen encoder deterministic (notably Transformer
                # dropout) while allowing the peak head to receive gradients.
                self.model.eval()
                self.model.peak_head.train()
                for start in range(0, len(shuffled), int(batch_size)):
                    batch = shuffled[start:start + int(batch_size)]
                    sequences = torch.tensor(
                        [sample[0] for sample in batch], dtype=torch.float32, device=self.device
                    )
                    targets = torch.tensor(
                        [sample[1] / 100.0 for sample in batch],
                        dtype=torch.float32, device=self.device,
                    )
                    optimizer.zero_grad()
                    _trajectory, predictions, _peak_time, _natural_time = self.model(sequences)
                    loss = self.loss_fn(predictions, targets)
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        self.model.peak_head.parameters(), float(gradient_clip)
                    )
                    optimizer.step()
                    epoch_losses.append(float(loss.item()))
                completed_epochs = epoch + 1
                last_loss = sum(epoch_losses) / len(epoch_losses) if epoch_losses else None
                self.model.eval()
                validation_mae = self.evaluate_peaks(validation_samples)
                if validation_mae is not None and validation_mae < best_mae - 1e-4:
                    best_mae = float(validation_mae)
                    best_state = copy.deepcopy(self.model.peak_head.state_dict())
                    stale_epochs = 0
                else:
                    stale_epochs += 1
                    if stale_epochs >= int(patience):
                        break
            self.model.peak_head.load_state_dict(best_state)
        finally:
            for parameter, flag in original_flags.items():
                parameter.requires_grad = flag
            self.model.eval()
        self.save()
        return {
            "epochs": completed_epochs,
            "best_validation_mae": None if best_mae == float("inf") else best_mae,
            "last_loss": last_loss,
            "train_samples": len(train_samples),
            "validation_samples": len(validation_samples),
        }

    def train_time_batch(self, samples, gradient_clip):
        if not samples:
            return None
        sequences = torch.tensor([sample[0] for sample in samples], dtype=torch.float32, device=self.device)
        peak_targets = torch.tensor(
            [sample[1]["minutes_to_peak"] / 720.0 for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        natural_targets = torch.tensor(
            [sample[1]["minutes_to_natural"] / 720.0 for sample in samples],
            dtype=torch.float32, device=self.device,
        )
        time_parameters = set(self.model.time_to_peak_head.parameters()) | set(
            self.model.time_to_natural_head.parameters()
        )
        original_flags = {parameter: parameter.requires_grad for parameter in self.model.parameters()}
        for parameter in self.model.parameters():
            parameter.requires_grad = parameter in time_parameters
        try:
            self.model.train()
            self.optimizer.zero_grad()
            _trajectory, _peak, peak_times, natural_times = self.model(sequences)
            loss = self.loss_fn(peak_times, peak_targets) + self.loss_fn(natural_times, natural_targets)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(time_parameters, float(gradient_clip))
            self.optimizer.step()
            return float(loss.item())
        finally:
            for parameter, flag in original_flags.items():
                parameter.requires_grad = flag
            self.model.eval()

    def evaluate(self, samples):
        if not samples:
            return None, None
        trajectory_error = 0.0
        peak_error = 0.0
        count = 0
        for sequence, trajectory, peak, _metadata in samples:
            predicted_trajectory, predicted_peak = self.predict(sequence)
            mask = _metadata.get("mask", [1.0] * 12)
            valid_errors = [
                abs(a - b) for a, b, valid in zip(predicted_trajectory, trajectory, mask)
                if valid
            ]
            if not valid_errors:
                continue
            trajectory_error += sum(valid_errors) / len(valid_errors)
            peak_error += abs(predicted_peak - peak)
            count += 1
        return (trajectory_error / count, peak_error / count) if count else (None, None)

    def evaluate_peaks(self, samples):
        if not samples:
            return None
        error = 0.0
        for sample in samples:
            sequence, peak = sample[0], sample[1]
            _trajectory, predicted_peak = self.predict(sequence)
            error += abs(predicted_peak - peak)
        return error / len(samples)

    def evaluate_masked_trajectories(self, samples):
        if not samples:
            return None
        total_error = 0.0
        total_hours = 0
        for sequence, trajectory, _peak, metadata in samples:
            predicted, _predicted_peak = self.predict(sequence)
            for prediction, actual, valid in zip(predicted, trajectory, metadata["mask"]):
                if valid:
                    total_error += abs(prediction - actual)
                    total_hours += 1
        return total_error / total_hours if total_hours else None

    def evaluate_h12(self, samples):
        if not samples:
            return None
        error = 0.0
        count = 0
        for sample in samples:
            sequence, trajectory, metadata = sample[0], sample[1], sample[3]
            if len(trajectory) < 12 or not metadata.get("mask", [1.0] * 12)[11]:
                continue
            predicted, _predicted_peak = self.predict(sequence)
            error += abs(predicted[11] - trajectory[11])
            count += 1
        return error / count if count else None

    def evaluate_times(self, samples):
        if not samples:
            return None, None
        peak_error = 0.0
        natural_error = 0.0
        for sequence, _trajectory, _peak, metadata in samples:
            details = self.predict_details(sequence)
            peak_error += abs(details["minutes_to_peak"] - metadata["minutes_to_peak"])
            natural_error += abs(details["minutes_to_natural"] - metadata["minutes_to_natural"])
        return peak_error / len(samples), natural_error / len(samples)

    def save(self):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".", suffix=".tmp", dir=folder)
        os.close(fd)
        try:
            torch.save(self.model.state_dict(), temp_path)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
