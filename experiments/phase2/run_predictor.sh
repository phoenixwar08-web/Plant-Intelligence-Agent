#!/bin/bash
set -euo pipefail

source /root/water-venv/venv/bin/activate
source /usr/local/Ascend/ascend-toolkit/set_env.sh
cd /root/water/wyc_training
exec python wyc_predictor_service.py
