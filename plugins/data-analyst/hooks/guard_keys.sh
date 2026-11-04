#!/usr/bin/env bash
# Runs guard_keys.py (the data-analyst key guard) with the first Python that works: python3, python, py.
# A missing interpreter or a Windows Store stub falls through to the next; none at all allows the call.
here="$(cd "$(dirname "$0")" && pwd)"
input="$(cat)"
for c in python3 python py; do
  command -v "$c" >/dev/null 2>&1 || continue
  err="$(printf '%s' "$input" | "$c" -I "$here/guard_keys.py" 2>&1 >/dev/null)"
  r=$?
  if [ "$r" -eq 2 ]; then printf '%s\n' "$err" >&2; exit 2; fi
  if [ "$r" -eq 0 ]; then exit 0; fi
done
exit 0
