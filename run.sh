#!/bin/bash
# Start the WMS Simulator. Run from the project folder.
# Assumes a virtual environment named "wms" exists (python3 -m venv wms).
cd "$(dirname "$0")"

if [ -d "wms" ]; then
  source wms/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
fi

echo "Starting WMS Simulator on http://127.0.0.1:8000"
echo "Make sure the AutoStore WMS SDK is running with a simulation started."
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
