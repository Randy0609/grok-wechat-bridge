#!/usr/bin/env sh
# Reproducible offline build + test. Python >= 3.11 standard library only.
set -eu
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export PYTHONDONTWRITEBYTECODE=1
python3 --version
python3 -m compileall -q -f src tests >/dev/null
python3 -m unittest discover -s tests -t tests -v
python3 -m grok_wechat_bridge directions
python3 -m grok_wechat_bridge selftest >/dev/null
echo "check.sh: all steps passed"
