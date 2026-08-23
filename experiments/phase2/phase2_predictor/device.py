import logging
import os
import subprocess
import sys
import textwrap

try:
    import torch
except ImportError:  # pragma: no cover - callers also handle missing torch
    torch = None


NPU_ENV_PATHS = [
    "/usr/local/Ascend/ascend-toolkit/latest/python/site-packages",
    "/usr/local/Ascend/ascend-toolkit/8.0.RC1/python/site-packages",
]


def _wants_npu(device_name):
    return str(device_name or "cpu").lower().startswith("npu")


def _with_default_npu_env(env):
    env = dict(env)
    python_paths = [path for path in NPU_ENV_PATHS if os.path.isdir(path)]
    if python_paths:
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(python_paths + ([current] if current else []))
    return env


def _npu_selftest(device_name, timeout_seconds):
    code = textwrap.dedent(
        """
        import os
        import sys
        import torch
        import torch_npu

        device = sys.argv[1]
        if not hasattr(torch, "npu") or not torch.npu.is_available():
            os._exit(2)
        torch.npu.set_device(device)
        tensor = torch.ones((1,), dtype=torch.float32, device=device)
        result = float((tensor + 1).cpu().item())
        if result != 2.0:
            os._exit(3)
        os.write(1, b"ok\\n")
        os._exit(0)
        """
    )
    env = _with_default_npu_env(os.environ)
    return subprocess.run(
        [sys.executable, "-c", code, str(device_name)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=float(timeout_seconds),
        check=False,
    )


def resolve_torch_device(requested_device, logger=None, timeout_seconds=12):
    """Return a usable torch device string, falling back to CPU if NPU is not healthy."""
    logger = logger or logging.getLogger(__name__)
    requested = str(requested_device or "cpu").strip() or "cpu"
    if torch is None:
        return "cpu", {"requested_device": requested, "active_device": "cpu", "reason": "torch_missing"}
    if not _wants_npu(requested):
        return requested, {"requested_device": requested, "active_device": requested, "reason": "requested"}

    try:
        result = _npu_selftest(requested, timeout_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("NPU self-test timed out after %ss; falling back to CPU", timeout_seconds)
        return "cpu", {"requested_device": requested, "active_device": "cpu", "reason": "npu_selftest_timeout"}
    except Exception as exc:
        logger.warning("NPU self-test could not start; falling back to CPU: %s", exc)
        return "cpu", {"requested_device": requested, "active_device": "cpu", "reason": "npu_selftest_error"}

    if result.returncode != 0:
        logger.warning(
            "NPU self-test failed rc=%s; falling back to CPU. stderr=%s",
            result.returncode,
            result.stderr.strip()[-500:],
        )
        return "cpu", {
            "requested_device": requested,
            "active_device": "cpu",
            "reason": "npu_selftest_failed",
            "returncode": result.returncode,
        }

    try:
        import torch_npu  # noqa: F401
        torch.npu.set_device(requested)
        return requested, {"requested_device": requested, "active_device": requested, "reason": "npu_ready"}
    except Exception as exc:
        logger.warning("NPU activation failed after self-test; falling back to CPU: %s", exc)
        return "cpu", {"requested_device": requested, "active_device": "cpu", "reason": "npu_activation_failed"}
