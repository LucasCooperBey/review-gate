#!/usr/bin/env bash
# Installs review-gate as the pre-push hook of the CURRENT git repo.
# Refuses to overwrite an existing hook (chain it manually instead — see README).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd -P)"
GITDIR="$(git rev-parse --git-dir 2>/dev/null)" || {
  echo "install.sh: run this from inside the git repo you want gated." >&2
  exit 1
}
HOOK="$GITDIR/hooks/pre-push"

if [ -e "$HOOK" ]; then
  echo "install.sh: $HOOK already exists — refusing to overwrite." >&2
  echo "Chain it manually by adding this line to your existing hook:" >&2
  echo "  python3 \"$HERE/review_gate.py\" --pre-push \"\$@\" < /dev/stdin" >&2
  exit 1
fi

mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<EOF
#!/bin/bash
# review-gate pre-push (installed by review-gate/install.sh).
# Fails OPEN by design: if python3 or the gate is missing it skips rather than
# blocking every push. Escape hatches:
#   REVIEW_GATE_SKIP=1 git push    (skip just the review)
#   git push --no-verify           (skip ALL pre-push hooks)
set -uo pipefail
[ "\${REVIEW_GATE_SKIP:-0}" = "1" ] && exit 0
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HERE/review_gate.py" ] || exit 0
exec python3 "$HERE/review_gate.py" --pre-push "\$@"
EOF
chmod +x "$HOOK"
echo "review-gate installed: $HOOK"
echo "Every 'git push' from this repo now gets a fresh-context review."
