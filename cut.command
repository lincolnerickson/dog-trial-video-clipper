#!/usr/bin/env bash
# Batch cutter wrapper on macOS. Example:
#   bash cut.command --video trial.mp4 --csv clips.csv --out clips
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "No virtual environment found (.venv). Run setup_mac.command first."
  exit 1
fi
exec .venv/bin/python cutter.py "$@"
