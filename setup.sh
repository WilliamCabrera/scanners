#!/usr/bin/env bash
set -e

VENV=".venv"

echo "Creating virtual environment at $VENV ..."
python3 -m venv "$VENV"

echo "Installing base dependencies ..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r requirements/base.txt --quiet

echo ""
echo "Done. To activate the environment:"
echo "  source $VENV/bin/activate"
echo ""
echo "Then run the scanner:"
echo "  python main.py"
echo ""
echo "Optional provider dependencies:"
echo "  pip install -r requirements/moomoo.txt"
echo "  pip install -r requirements/ibkr.txt"
echo "  pip install -r requirements/webull.txt"
