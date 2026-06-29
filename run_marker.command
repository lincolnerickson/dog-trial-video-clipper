#!/usr/bin/env bash
# Launch the marking tool on macOS.
# First time on this Mac: run setup_mac.command once to create the .venv.
# You can also pass a video path:  bash run_marker.command /path/to/trial.mp4
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No virtual environment found (.venv)."
  echo "Run the one-time setup first:  bash setup_mac.command"
  read -n 1 -s -r -p "Press any key to close..."
  echo
  exit 1
fi

.venv/bin/python marker.py "$@"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  echo "marker.py exited with status $status (see the error above)."
  read -n 1 -s -r -p "Press any key to close..."
  echo
fi
