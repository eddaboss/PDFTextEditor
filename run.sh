#!/usr/bin/env bash
# Launch the PDF Text Editor from the project venv.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m pdftexteditor.main
