# Changelog

## 2026-11-12

- A timeout is retried once at 2.5 times that attempt's budget. The default budget scales from 180 seconds by one second per 500 diff bytes, up to 300, and an explicit timeout is capped at 600 seconds per attempt. All attempts and reviewers share a 900 second wall, and a retry is skipped when under 30 seconds remain. A critical path with no usable verdict after every attempt and reviewer blocks the push with exit 3 (BLOCKED_NO_VERDICT). Other diffs still fail open. --fail-open-always or REVIEW_GATE_FAIL_OPEN_ALWAYS=1 restores the old allow-on-infra behavior.
- README: an Example section with a recorded cross-family REJECT (Codex as author, Claude as reviewer) on a change with two planted bugs.
- Add cross-family reviewers. `--reviewer auto|claude|codex|cmd` and `--author claude|codex|other` choose a reviewer from a different family than the one that wrote the change. Auto uses the author's own family only when no other family is installed or every other family hit a usage limit, and prints that the review is same-family.
- Add panel mode (`--reviewers` or `REVIEW_GATE_REVIEWERS`). The combined verdict is REJECT if any reviewer returns REJECT, otherwise WARN if any returns WARN or an infra error, otherwise PASS. Each reviewer is logged, plus one combined record with `agreement`.
- Send the Claude prompt on stdin (`claude -p --model M --output-format json`). The Claude reviewer gets read-only tools (Read, Grep, Glob), no MCP servers, and saves no session of its own, so text inside a diff cannot make it act. Add a Codex reviewer (`codex exec`, read-only sandbox, prompt on stdin, last message written to a file) and a `cmd` reviewer (`REVIEW_GATE_REVIEWER_CMD`).
- Treat a usage limit, rate limit, quota, 429, or "try again" reply as an infra WARN that names the limit, only when the reply has no verdict. A reply with a verdict always stands, so limit words in a REJECT or in the diff cannot let a push through. In auto mode, try the next reviewer and print which one ran, or that none was left.
- Keep only verdict, bugs, summary and e2e_present from the reviewer's reply, so a reply cannot mark itself as an infra error.
- A reviewer that cannot start (for example, no execute permission) is an infra WARN instead of a crash.
- Auto moves to the next reviewer after any infra error (a timeout, a reviewer that cannot start), not only a usage limit.
- An unknown `REVIEW_GATE_AUTHOR` value is named on screen instead of being dropped silently.
- `--stats` leaves docs-only SKIP rows and the panel's combined row out of the scored count, and prints the skipped count.
- Skip docs-only changes: print the skip line, list the non-source files, log SKIP, and exit 0.
- `--stats` adds a per-reviewer line (reviews, REJECT count, finding rate) and, when panel records exist, a disagreement count. `--round N` (default 1) is stored on each spine record.
