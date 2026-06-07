#!/bin/zsh
# Launch WinUSB Creator, creating the venv and installing dependencies if needed.
# ${0:A:h} resolves symlinks: allows invoking it via a link on the PATH (winusb).
set -e
cd "${0:A:h}"

if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import rich" 2>/dev/null; then
  echo "Installing rich…"
  .venv/bin/pip install --quiet --disable-pip-version-check rich
fi

exec .venv/bin/python winusb.py "$@"
