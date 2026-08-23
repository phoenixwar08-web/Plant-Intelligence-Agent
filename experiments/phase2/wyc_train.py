"""Independent Phase 2 Transformer trainer. It never starts or waits for Phase 3."""

import argparse
import json
import signal

from phase2_predictor.config import ConfigManager
from phase2_predictor.shadow_training import ShadowTrainer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one training and validation cycle")
    args = parser.parse_args()
    trainer = ShadowTrainer(ConfigManager())
    if args.once:
        print(json.dumps(trainer.process_once(), ensure_ascii=False, indent=2))
        return
    signal.signal(signal.SIGINT, trainer.stop)
    signal.signal(signal.SIGTERM, trainer.stop)
    trainer.run()


if __name__ == "__main__":
    main()
