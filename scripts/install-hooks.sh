#!/bin/sh
# Install Sentinel's git hooks. Run once per clone:  sh scripts/install-hooks.sh
#
# .git/hooks is not versioned, so a fresh clone has no hook until this runs.
# We symlink (not copy) so edits to scripts/pre-commit take effect immediately.
set -eu

root=$(git rev-parse --show-toplevel)
hooks_dir="$root/.git/hooks"
mkdir -p "$hooks_dir"
ln -sf ../../scripts/pre-commit "$hooks_dir/pre-commit"
chmod +x "$root/scripts/pre-commit"
echo "installed: .git/hooks/pre-commit -> scripts/pre-commit"

if [ ! -f "$root/.pii-patterns" ]; then
  echo "note: no .pii-patterns yet — cp .pii-patterns.example .pii-patterns and fill it in." >&2
fi
