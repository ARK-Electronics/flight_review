#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/app"
export PYTHONPATH=".:plot_app:plot_app/libevents/libs/python"
python -m pylint tornado_handlers/*.py serve.py plot_app/*.py download_logs.py \
  analysis_worker.py isolated_worker.py ops_backup.py backup_scheduler.py runtime_config.py sqlite_utils.py
