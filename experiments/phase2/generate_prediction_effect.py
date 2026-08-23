"""Generate prediction_effect.csv and prediction_effect_12h.csv immediately."""

from phase2_predictor.config import ConfigManager
from phase2_predictor.effect_report import build_prediction_effect_report


if __name__ == "__main__":
    config = ConfigManager().get()
    count = build_prediction_effect_report(config)
    print(
        "prediction_effect.csv and prediction_effect_12h.csv generated "
        f"with {count} valid result rows"
    )
