import csv
import os
import time
try:
    import resource
except ImportError:
    resource = None


def rss_mb():
    status_path = "/proc/self/status"
    if os.path.exists(status_path):
        try:
            with open(status_path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except (OSError, ValueError, IndexError):
            pass
    if resource is None:
        return 0.0
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / 1024.0


def append_resource_log(path, started, csv_rows, pending_tasks, backprops, paused):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    row = {
        "time": time.strftime("%Y/%m/%d %H:%M"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rss_mb": round(rss_mb(), 2),
        "csv_rows": csv_rows,
        "pending_tasks": pending_tasks,
        "backpropagations": backprops,
        "training_paused_for_memory": paused,
    }
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new:
            writer.writeheader()
        writer.writerow(row)
