import logging
import os
import tempfile

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
except ImportError:
    torch = None
    nn = None
    optim = None


if nn is not None:
    class LightweightEdgeTransformer(nn.Module):
        def __init__(self, input_dim=6, seq_len=12, hidden_dim=32):
            super().__init__()
            self.input_dim = input_dim
            self.seq_len = seq_len
            self.feature_proj = nn.Linear(input_dim, hidden_dim)
            self.q = nn.Linear(hidden_dim, hidden_dim)
            self.k = nn.Linear(hidden_dim, hidden_dim)
            self.v = nn.Linear(hidden_dim, hidden_dim)
            self.gate = nn.Sequential(nn.Linear(hidden_dim, hidden_dim), nn.Sigmoid())
            self.head = nn.Linear(hidden_dim, 1)
            self.raw_kp = nn.Parameter(torch.tensor(0.0))
            self.raw_damping = nn.Parameter(torch.tensor(-1.38))

        def forward_logits(self, values):
            values = self.feature_proj(values)
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
    def __init__(self, config, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.model = None
        self.status = "unavailable"
        self.device = config["model"]["device"]
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
                self.model = None
                self.status = "weights_missing"
                return
            try:
                state = torch.load(self.weights_path, map_location=self.device, weights_only=True)
            except TypeError:
                state = torch.load(self.weights_path, map_location=self.device)
            self.model.load_state_dict(state)
            self.model.eval()
            if config["training"]["enabled"]:
                self.optimizer = optim.Adam(
                    self.model.parameters(), lr=float(config["training"]["learning_rate"])
                )
                self.loss_fn = nn.BCEWithLogitsLoss()
            self.status = "loaded"
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
        logits = self.model.forward_logits(sequence)
        loss = self.loss_fn(logits, target)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=float(gradient_clip))
        self.optimizer.step()
        self.model.eval()
        return float(loss.item())

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
