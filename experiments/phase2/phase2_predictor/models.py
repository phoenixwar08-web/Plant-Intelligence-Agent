import logging
import os
import tempfile

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim

    from .device import resolve_torch_device
except ImportError:
    torch = None
    nn = None
    optim = None


if nn is not None:
    class LightweightEdgeTransformer(nn.Module):
        def __init__(self, input_dim=9, seq_len=12, hidden_dim=32):
            super().__init__()
            self.input_dim = input_dim
            self.seq_len = seq_len
            self.feature_proj = nn.Linear(input_dim, hidden_dim)
            self.position = nn.Parameter(torch.zeros(1, seq_len, hidden_dim))
            self.q = nn.Linear(hidden_dim, hidden_dim)
            self.k = nn.Linear(hidden_dim, hidden_dim)
            self.v = nn.Linear(hidden_dim, hidden_dim)
            self.gate = nn.Sequential(nn.Linear(hidden_dim, hidden_dim), nn.Sigmoid())
            self.head = nn.Linear(hidden_dim, 1)
            self.raw_kp = nn.Parameter(torch.tensor(0.0))
            self.raw_damping = nn.Parameter(torch.tensor(-1.38))

        def forward_logits(self, values):
            values = self.feature_proj(values) + self.position[:, :values.shape[1], :]
            query = torch.relu(self.q(values)) + 1e-6
            key = torch.relu(self.k(values)) + 1e-6
            value = self.v(values)
            context = torch.matmul(key.transpose(1, 2), value)
            output = torch.matmul(query, context) / (torch.sum(key, dim=1, keepdim=True) + 1e-6)
            output = output * self.gate(output)
            return self.head(torch.mean(output, dim=1)).squeeze(1)

        def forward(self, values):
            return torch.sigmoid(self.forward_logits(values)), None
else:
    LightweightEdgeTransformer = None


class ModelRuntime:
    def __init__(self, config, logger=None, allow_fresh=False):
        self.logger = logger or logging.getLogger(__name__)
        self.model = None
        self.status = "unavailable"
        model_cfg = config["model"]
        self.requested_device = model_cfg.get("device", "cpu")
        self.device, self.device_status = resolve_torch_device(
            self.requested_device, self.logger, model_cfg.get("npu_selftest_timeout_seconds", 12)
        )
        self.weights_path = config["paths"]["model_weights"]
        self.optimizer = None
        self.loss_fn = None
        if torch is None:
            self.logger.warning("PyTorch is unavailable; physical fallback enabled")
            return
        try:
            torch.set_num_threads(int(config["model"].get("num_threads", 1)))
            try:
                torch.set_num_interop_threads(int(config["model"].get("num_interop_threads", 1)))
            except RuntimeError:
                pass
            model_cfg = config["model"]
            self.model = LightweightEdgeTransformer(
                model_cfg["input_dim"], model_cfg["sequence_length"], model_cfg["hidden_dim"]
            ).to(self.device)
            if not os.path.exists(self.weights_path):
                if not allow_fresh:
                    self.model = None
                    self.status = "weights_missing"
                    return
                self.status = "fresh"
            else:
                try:
                    state = torch.load(self.weights_path, map_location=self.device, weights_only=True)
                except TypeError:
                    state = torch.load(self.weights_path, map_location=self.device)
                try:
                    self.model.load_state_dict(state)
                    self.status = "loaded"
                except RuntimeError:
                    if not allow_fresh:
                        raise
                    self.logger.warning("Existing weights are incompatible; starting a fresh model")
                    self.status = "fresh_incompatible_weights"
            self.model.eval()
            if config["training"]["enabled"]:
                self.optimizer = optim.Adam(
                    self.model.parameters(), lr=float(config["training"]["learning_rate"])
                )
                self.loss_fn = nn.SmoothL1Loss(beta=0.05)
        except Exception:
            self.logger.exception("Failed to load Transformer model; physical fallback enabled")
            self.model = None
            self.status = "load_failed"

    def predict_percent(self, normalized_history):
        if self.model is None or torch is None:
            return None
        try:
            sequence = torch.tensor([normalized_history], dtype=torch.float32, device=self.device)
            with torch.inference_mode():
                prediction, _ = self.model(sequence)
            value = float(prediction.item() * 100.0)
            return value if 0.0 <= value <= 100.0 else None
        except Exception:
            self.logger.exception("Transformer inference failed")
            return None

    def supports_training(self):
        return self.model is not None and self.optimizer is not None and self.loss_fn is not None

    def train_sample(self, normalized_history, target_percent, gradient_clip):
        if not self.supports_training():
            return None
        sequence = torch.tensor([normalized_history], dtype=torch.float32, device=self.device)
        target = torch.tensor(
            [max(0.0, min(float(target_percent) / 100.0, 1.0))],
            dtype=torch.float32,
            device=self.device
        )
        self.model.train()
        self.optimizer.zero_grad()
        prediction, _ = self.model(sequence)
        loss = self.loss_fn(prediction, target)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def train_batch(self, samples, gradient_clip):
        if not self.supports_training() or not samples:
            return None
        sequences = torch.tensor([item[0] for item in samples], dtype=torch.float32, device=self.device)
        targets = torch.tensor(
            [max(0.0, min(float(item[1]) / 100.0, 1.0)) for item in samples],
            dtype=torch.float32,
            device=self.device,
        )
        self.model.train()
        self.optimizer.zero_grad()
        predictions, _ = self.model(sequences)
        loss = self.loss_fn(predictions, targets)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

    def evaluate_samples(self, samples, batch_size=256):
        if self.model is None or torch is None or not samples:
            return None
        total_error = 0.0
        count = 0
        self.model.eval()
        with torch.inference_mode():
            for start in range(0, len(samples), int(batch_size)):
                batch = samples[start:start + int(batch_size)]
                tensor = torch.tensor([item[0] for item in batch], dtype=torch.float32, device=self.device)
                prediction, _ = self.model(tensor)
                targets = torch.tensor([item[1] for item in batch], dtype=torch.float32, device=self.device)
                total_error += float(torch.sum(torch.abs(prediction * 100.0 - targets)).item())
                count += len(batch)
        return total_error / count

    def save_weights(self):
        if self.model is None or torch is None:
            return False
        folder = os.path.dirname(self.weights_path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=os.path.basename(self.weights_path) + ".", suffix=".tmp", dir=folder
        )
        os.close(fd)
        try:
            torch.save(self.model.state_dict(), temp_path)
            os.replace(temp_path, self.weights_path)
            return True
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
