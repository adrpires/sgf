#!/bin/sh
set -eu
mkdir -p "$(dirname "$SGF_OUT")"
if [ ! -s "$SGF_OUT" ]; then
  cp /app/placeholder.html "$SGF_OUT"
fi
exec python /app/sync_sgf.py --a-cada "${SGF_A_CADA:-30}"
