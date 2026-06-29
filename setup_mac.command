#!/usr/bin/env bash
# One-time macOS setup for the Dog Trial Video Clipper.
# Creates the .venv and installs dependencies. Run it ONCE:
#   - in Terminal:        bash setup_mac.command
#   - or double-click it in Finder (works once it's marked executable)
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 was not found."
  echo "Install it first — 'brew install python', or from https://www.python.org/downloads/ —"
  echo "then run this again."
  exit 1
fi
echo "Using $("$PY" --version) at $(command -v "$PY")"

# A Windows .venv (it has Scripts/ instead of bin/) can't be used here; rebuild fresh.
if [ -d .venv ] && [ ! -x .venv/bin/python ]; then
  echo "Removing an existing non-macOS .venv ..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment in .venv ..."
  "$PY" -m venv .venv
fi

echo "Installing dependencies ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# Make the launchers double-clickable in Finder from now on.
chmod +x run_marker.command cut.command setup_mac.command 2>/dev/null || true

echo
echo "Setup complete."
echo "Launch the marker by double-clicking run_marker.command (or: bash run_marker.command)."
