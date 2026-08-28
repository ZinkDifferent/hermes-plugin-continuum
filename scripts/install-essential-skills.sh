#!/bin/bash
# Install continuum's bundled essential/core skills into ~/.hermes/skills/.
# Additive: never overwrites an existing skill of the same name.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)/skills"
DST="${HERMES_HOME:-$HOME/.hermes}/skills"
mkdir -p "$DST"
for skill_dir in "$SRC"/*/; do
  name=$(basename "$skill_dir")
  if [ -e "$DST/$name" ]; then
    echo "skip (exists): $name"
  else
    cp -R "$skill_dir" "$DST/$name"
    echo "installed: $name"
  fi
done