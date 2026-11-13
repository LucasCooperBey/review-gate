#!/usr/bin/env python3
"""review_gate: fresh-context adversarial review gate for code changes.

Classifies a diff, runs a fresh-context reviewer (Claude, Codex, a panel, or a
command), parses a structured verdict, logs it to an append-only spine, and
returns an exit code suitable for a git pre-push hook. REJECT is exit 1. A
critical path with no usable verdict is exit 3.

Fresh context is the whole point: each review is a separate process that sees
ONLY the diff + changed-file list + (optional) acceptance spec, never the
authoring conversation. Auto mode picks a reviewer from a different model
family than the author when one is installed.

A timeout is retried once at 2.5 times that attempt's budget. The default
budget scales with diff size. Every attempt and every reviewer share a 900
second wall. If no usable verdict comes back, the gate allows the push, except
on a critical path (auth, payment, migration, secrets), where it blocks.
--fail-open-always restores the blanket allow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# -- locations ------------------------------------------------------------------
# Spine dir: $REVIEW_GATE_SPINE_DIR > a "spine/" directory next to this script.
# The spine is a plain append-only JSONL log; the envelope shape follows the
# spine pattern (github.com/LucasCooperBey/spine-pattern).
DEFAULT_SPINE_DIR = os.environ.get("REVIEW_GATE_SPINE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "spine")
MAX_DIFF_BYTES = 60_000
ZERO_SHA = "0" * 40

# -- reviewer budget ------------------------------------------------------------
# A push is synchronous. MAX_TOTAL_TIMEOUT is the wall across every attempt and
# every reviewer, so the timeout retry cannot turn the push into a long hang.
BASE_TIMEOUT = 180
MAX_TIMEOUT = 600            # ceiling for any single attempt
MAX_TOTAL_TIMEOUT = 900      # hard wall across all attempts and reviewers
TIMEOUT_BYTES_PER_SEC = 500  # +1s of default budget per this many diff bytes
INFRA_RETRY_MULT = 2.5       # one retry at this multiple of the attempt budget
DEFAULT_TIMEOUT_CAP = 300    # ceiling for the scaled default, not for --timeout
RETRY_MIN_REMAINING = 30     # skip the retry when the wall has less than this left

# -- classification rules -------------------------------------------------------
CODE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".rb",
    ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php",
    ".scala", ".sh", ".bash", ".zsh", ".sql", ".vue", ".svelte", ".prisma",
}
# Paths that warrant the stronger (Opus) reviewer.
CRITICAL_RE = re.compile(
    r"(auth|login|logout|session|password|credential|secret|token|oauth|"
    r"payment|billing|stripe|checkout|invoice|charge|"
    r"migrat|drizzle|alembic|"
    r"\.env|webhook)",
    re.IGNORECASE,
)
# Paths whose behavior is user-facing -> E2E evidence expected, not unit tests alone.
APP_RE = re.compile(
    r"(^|/)(app|pages|routes?|views|screens|components)(/|$)|"
    r"\.(tsx|jsx|vue|svelte)$|route|handler|controller|endpoint",
    re.IGNORECASE,
)

VERDICT_SYNONYMS = {
    "pass": "PASS", "ok": "PASS", "approve": "PASS", "approved": "PASS", "lgtm": "PASS",
    "reject": "REJECT", "block": "REJECT", "blocked": "REJECT", "fail": "REJECT", "failed": "REJECT",
    "warn": "WARN", "warning": "WARN", "caution": "WARN",
}
CANONICAL_VERDICTS = {"PASS", "REJECT", "WARN"}


# =====================================================================================
# PURE LOGIC (unit-tested in test_review_gate.py)
# =====================================================================================

def classify_files(paths: list[str]) -> dict:
    """Split changed paths into code/docs and flag critical + e2e-required."""
    code_files, doc_files = [], []
    critical = False
    e2e_required = False
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        (code_files if ext in CODE_EXTS else doc_files).append(p)
        if CRITICAL_RE.search(p):
            critical = True
        if APP_RE.search(p):
            e2e_required = True
    has_code = len(code_files) > 0
    if not has_code:
        # A docs-only change is neither code-critical nor E2E-relevant.
        critical = False
        e2e_required = False
    return {
        "code_files": code_files,
        "doc_files": doc_files,
        "has_code": has_code,
        "critical": critical,
        "e2e_required": e2e_required,
    }


def choose_model(classification: dict, override: str | None = None) -> str:
    """Sonnet by default; Opus on critical paths. An explicit override always wins."""
    if override in ("sonnet", "opus"):
        return override
    return "opus" if classification.get("critical") else "sonnet"


def _iter_json_objects(text: str):
    """Yield candidate top-level {...} substrings via brace matching (string-aware)."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None


REVIEW_KEYS = ("verdict", "bugs", "summary", "e2e_present")


def parse_verdict(text: str) -> dict:
    """Extract the reviewer's verdict from possibly prose-wrapped output.

    Robust to: clean JSON, fenced JSON, prose around JSON, and multiple JSON blocks
    (the LAST block carrying a `verdict` wins). Unparseable -> WARN + parse_error.
    """
    text = text or ""
    # Prefer the LAST block whose verdict value is actually RECOGNIZED. This way a
    # chatty reviewer that echoes the schema (`{"verdict":"PASS|REJECT|WARN"}`) or emits
    # a non-canonical value can't override a real REJECT, and "couldn't understand the
    # reviewer" is reported as parse_error (callers can fail CLOSED on it) rather than
    # silently masquerading as a clean WARN.
    last_recognized = None  # (obj, canonical_verdict)
    for blob in _iter_json_objects(text):
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if not (isinstance(obj, dict) and "verdict" in obj):
            continue
        raw = str(obj.get("verdict", "")).strip().lower()
        canon = VERDICT_SYNONYMS.get(raw)
        if canon is None and raw.upper() in CANONICAL_VERDICTS:
            canon = raw.upper()
        if canon is not None:
            last_recognized = (obj, canon)
    if last_recognized is None:
        return {"verdict": "WARN", "bugs": [],
                "summary": "reviewer output had no recognizable verdict", "parse_error": True}
    obj, verdict = last_recognized
    bugs = obj.get("bugs") or []
    if not isinstance(bugs, list):
        bugs = [str(bugs)]
    # Only the review keys come from the reviewer. infra_error, limit_marker, panel,
    # reviewer, model and round are set by this gate, so a reply cannot mark itself
    # as an infra error and be dropped.
    out = {key: obj[key] for key in REVIEW_KEYS if key in obj}
    out["verdict"] = verdict
    out["bugs"] = bugs
    out["summary"] = str(obj.get("summary") or "")
    return out


def had_usable_verdict(verdict: dict) -> bool:
    """True when the reviewer actually rendered a verdict.

    A timeout, a missing binary, a bad exit, and unparseable output are not
    usable. Neither may be treated as a completed review.
    """
    return not (verdict.get("infra_error") or verdict.get("parse_error"))


def default_timeout(diff_bytes: int) -> int:
    """Budget used when --timeout is omitted.

    180 seconds plus one second per 500 diff bytes, clamped to
    [BASE_TIMEOUT, DEFAULT_TIMEOUT_CAP]. An explicit --timeout is separate
    and may go up to MAX_TIMEOUT.
    """
    scaled = BASE_TIMEOUT + max(0, int(diff_bytes)) // TIMEOUT_BYTES_PER_SEC
    return max(BASE_TIMEOUT, min(DEFAULT_TIMEOUT_CAP, scaled))


def clamp_attempt_budget(timeout: int, warn: bool = False) -> int:
    """Clamp one attempt to [1, MAX_TIMEOUT]. warn=True is the CLI boundary."""
    raw = int(timeout)
    clamped = max(1, min(raw, MAX_TIMEOUT))
    if warn and clamped != raw:
        print(
            f"review-gate: --timeout {raw}s is outside 1 to {MAX_TIMEOUT} seconds; "
            f"clamping to {clamped}s.",
            file=sys.stderr,
        )
    return clamped


def critical_paths(paths: list[str]) -> list[str]:
    """Critical files named on a block, at most five, in the given order."""
    return [path for path in paths if CRITICAL_RE.search(path)][:5]


def critical_block_line(paths: list[str]) -> str:
    """Stderr line when a critical path produced no usable verdict."""
    shown = ", ".join(critical_paths(paths))
    return (
        "review-gate: review could not finish for critical files "
        f"({shown}). Override this gate with --fail-open-always, "
        "or skip all hooks with git push --no-verify."
    )


def exit_code_for(verdict: dict, critical: bool = False,
                  fail_open_always: bool = False, parts: list | None = None) -> int:
    """REJECT blocks (1). No usable verdict on a critical path blocks (3).

    PASS, WARN, and a non-critical infra failure allow (0). parts is the
    panel's own verdicts; any usable member means the panel answered.
    """
    if verdict.get("verdict") == "REJECT":
        return 1
    if not critical or fail_open_always:
        return 0
    group = parts if parts is not None else [verdict]
    if any(had_usable_verdict(part) for part in group):
        return 0
    return 3


# =====================================================================================
# GIT + REVIEWER ORCHESTRATION (integration-tested)
# =====================================================================================

def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    # errors="replace": diffs touching binary-ish files (e.g. model blobs in a
    # checkpoint range) contain invalid utf-8; strict decode crashed the whole gate
    # once (fail-open, so that push went entirely UNREVIEWED). Mojibake in a diff
    # hunk is reviewable; a dead gate reviews nothing.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          errors="replace")


def compute_range(localsha: str, remotesha: str) -> str:
    """Range of commits being pushed. New branch (remotesha=0) -> merge-base with the
    remote head, falling back to the tip commit only so we never review the world."""
    if remotesha and remotesha != ZERO_SHA:
        return f"{remotesha}..{localsha}"
    mb = git("merge-base", localsha, "origin/HEAD").stdout.strip()
    if mb:
        return f"{mb}..{localsha}"
    return f"{localsha}~1..{localsha}"


def changed_files_for_range(rng: str) -> list[str]:
    r = git("diff", "--name-only", rng)
    return [l for l in r.stdout.splitlines() if l.strip()]


def diff_for_range(rng: str) -> str:
    return git("diff", "--no-color", rng).stdout


def build_prompt(diff_text: str, files: list[str], classification: dict,
                 spec: str | None, truncated: bool) -> str:
    schema = (
        '{"verdict":"PASS|REJECT|WARN",'
        '"bugs":[{"file":"path","line":"n","severity":"high|med|low","issue":"what breaks"}],'
        '"e2e_present":true,"summary":"one line"}'
    )
    rules = (
        "Verdict rules:\n"
        "- REJECT if there is ANY high-severity correctness or security bug (data loss, "
        "auth bypass, injection, null/undefined deref on a hot path, broken control flow, "
        "race, money/units error).\n"
        "- WARN if only medium/low concerns, OR an app-facing behavior changed without "
        "E2E evidence in the diff (unit tests alone are NOT sufficient evidence).\n"
        "- PASS only if you found nothing actionable.\n"
        "When uncertain whether something is a real bug, lean toward flagging it."
    )
    parts = [
        "You are an adversarial senior code reviewer in a FRESH context. You do not know "
        "who wrote this code or why. Assume the author is biased; you are not. Find what "
        "will break in production. Do not praise. Report only concrete, actionable findings.",
        "",
        f"Changed files ({len(files)}): {', '.join(files[:40])}"
        + (" ..." if len(files) > 40 else ""),
        f"Critical path (auth/payment/migration): {classification['critical']}",
        f"E2E evidence expected for this change: {classification['e2e_required']}",
    ]
    if spec:
        parts += ["", "ACCEPTANCE SPEC (check the diff satisfies this):", spec.strip()[:4000]]
    parts += [
        "",
        "DIFF" + (" (TRUNCATED -- judge only what you can see, and say so)" if truncated else "") + ":",
        diff_text,
        "",
        rules,
        "",
        "Respond with ONLY a single JSON object, no prose around it:",
        schema,
    ]
    return "\n".join(parts)


def limit_marker(text: str | None) -> str | None:
    """Return the first usage-limit phrase in text, if any.

    Matched case-insensitively, in the order: usage limit, rate limit, quota,
    429, try again. 429 must be its own number so a byte count cannot trip it.
    """
    lowered = (text or "").lower()
    for marker in ("usage limit", "rate limit", "quota"):
        if marker in lowered:
            return marker
    if re.search(r"(?<![0-9])429(?![0-9])", lowered):
        return "429"
    if "try again" in lowered:
        return "try again"
    return None


def claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def codex_bin() -> str:
    return shutil.which("codex") or "codex"


def _claude_installed() -> bool:
    if shutil.which("claude"):
        return True
    fallback = os.path.expanduser("~/.local/bin/claude")
    return os.path.isfile(fallback) and os.access(fallback, os.X_OK)


def reviewer_available(name: str) -> bool:
    """True when this machine can launch that reviewer."""
    if name == "claude":
        return _claude_installed()
    if name == "codex":
        return shutil.which("codex") is not None
    if name == "cmd":
        return bool(os.environ.get("REVIEW_GATE_REVIEWER_CMD", "").strip())
    return False


def _infra(reviewer: str, model: str, summary: str, **extra) -> dict:
    out = {
        "verdict": "WARN",
        "bugs": [],
        "infra_error": True,
        "reviewer": reviewer,
        "model": model,
        "summary": summary,
    }
    out.update(extra)
    return out


def _from_process(reviewer: str, model: str, text: str, rc: int, err: str,
                  raw: str, classification: dict) -> dict:
    verdict = parse_verdict(text)
    # A recognized verdict always stands. Limit words are read only when the reviewer
    # gave no verdict: the reply and Codex's stderr can quote the diff, so a REJECT
    # that mentions a rate limit, or a diff with "429" in it, must still block.
    if verdict.get("parse_error"):
        marker = limit_marker(raw)
        if marker:
            return _infra(
                reviewer, model,
                f"reviewer hit {marker} -- allowed (fail-open)",
                limit_marker=marker,
            )
        if rc != 0 and not (text or "").strip():
            return _infra(
                reviewer, model,
                f"reviewer exited {rc}: {(err or '')[:200]} -- allowed (fail-open)",
            )
    verdict["model"] = model
    verdict["reviewer"] = reviewer
    verdict.setdefault("e2e_required", classification.get("e2e_required"))
    return verdict


class _Wall:
    """Elapsed-time ceiling shared by every attempt and every reviewer."""

    def __init__(self, total: int = MAX_TOTAL_TIMEOUT) -> None:
        self.deadline = time.monotonic() + total

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def _budget_for_attempt(timeout: int, wall: _Wall) -> int:
    """Seconds this attempt may run, or 0 when the wall is already spent."""
    remaining = wall.remaining()
    if remaining < 1:
        return 0
    return min(int(timeout), MAX_TIMEOUT, int(remaining))


def _retry_budget(first: int, remaining: float) -> int:
    """Seconds for the one timeout retry, or 0 when the wall cannot spare it.

    The retry is skipped when under RETRY_MIN_REMAINING seconds are left.
    Otherwise the budget is 2.5x the attempt, and neither a single attempt
    nor the shared wall may be exceeded.
    """
    if remaining < RETRY_MIN_REMAINING:
        return 0
    allowed = min(int(first * INFRA_RETRY_MULT), MAX_TIMEOUT, remaining)
    return int(allowed)


def _mark_retry(verdict: dict, first: int, second: int) -> dict:
    verdict["retried"] = True
    verdict["attempt_budgets"] = [first, second]
    return verdict


def _capture(reviewer: str, model: str, classification: dict, timeout: int,
             call, missing_summary: str, wall: _Wall) -> dict:
    """Run one reviewer. A TimeoutExpired is retried once; other infra is not."""
    first = _budget_for_attempt(timeout, wall)
    if first < 1:
        return _infra(
            reviewer, model,
            "no time left under the 900s review wall -- no review performed",
        )
    try:
        text, rc, err, raw = call(first)
    except subprocess.TimeoutExpired:
        return _after_timeout(
            reviewer, model, classification, call, missing_summary, wall, first,
        )
    except FileNotFoundError:
        return _infra(reviewer, model, missing_summary)
    except OSError as exc:
        return _infra(
            reviewer, model,
            f"reviewer could not start: {exc} -- allowed (fail-open)",
        )
    return _from_process(reviewer, model, text, rc, err, raw, classification)


def _after_timeout(reviewer: str, model: str, classification: dict, call,
                   missing_summary: str, wall: _Wall, first: int) -> dict:
    second = _retry_budget(first, wall.remaining())
    if second < 1:
        return _infra(
            reviewer, model,
            f"reviewer timed out after {first}s -- no review performed",
        )
    print(
        f"review-gate: reviewer timed out after {first}s; retrying once at {second}s.",
        file=sys.stderr,
    )
    try:
        text, rc, err, raw = call(second)
    except subprocess.TimeoutExpired:
        verdict = _infra(
            reviewer, model,
            f"reviewer timed out after {first}s -- no review performed",
        )
    except FileNotFoundError:
        verdict = _infra(reviewer, model, missing_summary)
    except OSError as exc:
        verdict = _infra(
            reviewer, model,
            f"reviewer could not start: {exc} -- allowed (fail-open)",
        )
    else:
        verdict = _from_process(reviewer, model, text, rc, err, raw, classification)
    return _mark_retry(verdict, first, second)


def run_reviewer(prompt: str, model: str, timeout: int) -> tuple[str, int, str, str]:
    """Run a fresh Claude review. Returns (result_text, returncode, stderr, raw).

    The prompt is stdin, not an argv element:
    claude -p --model M --output-format json
    `claude --output-format json` wraps the reply:
    {"type":"result","result":"<text>",...}

    The diff under review was written by a model and may carry text aimed at
    the reviewer, so the reviewer gets read-only tools and no MCP servers, and
    the call saves no session of its own.
    """
    cmd = [claude_bin(), "-p", "--model", model, "--output-format", "json",
           "--tools", "Read,Grep,Glob", "--strict-mcp-config", "--no-session-persistence"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, errors="replace")
    out = proc.stdout or ""
    try:
        env = json.loads(out)
        result_text = env.get("result", out) if isinstance(env, dict) else out
    except Exception:
        result_text = out
    if not isinstance(result_text, str):
        result_text = json.dumps(result_text)
    err = proc.stderr or ""
    raw = "\n".join([out, err, result_text])
    return result_text, proc.returncode, err, raw


def run_codex_reviewer(prompt: str, model: str | None, timeout: int) -> tuple[str, int, str, str]:
    """Run a fresh Codex review. Returns (result_text, returncode, stderr, raw)."""
    # codex-cli 0.163.0
    # codex --ask-for-approval never exec --sandbox read-only --output-last-message <file> [--model MODEL] -
    # Non-interactive exec. --ask-for-approval never is the top-level flag from
    # `codex --help` so the process does not wait on a person. --sandbox read-only
    # is the documented read-only sandbox. The trailing "-" reads the prompt on
    # stdin. --output-last-message writes the last message to the file. --model
    # is omitted when no Codex model is set.
    with tempfile.TemporaryDirectory(prefix="review-gate-codex-") as tmp:
        out_path = os.path.join(tmp, "last-message.txt")
        cmd = [
            codex_bin(),
            "--ask-for-approval", "never",
            "exec",
            "--sandbox", "read-only",
            "--output-last-message", out_path,
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
        last = ""
        try:
            with open(out_path, encoding="utf-8", errors="replace") as handle:
                last = handle.read()
        except OSError:
            last = ""
        err = proc.stderr or ""
        stdout = proc.stdout or ""
        text = last if last.strip() else stdout
        raw = "\n".join([stdout, err, last])
        return text, proc.returncode, err, raw


def run_cmd_reviewer(prompt: str, command: str, timeout: int) -> tuple[str, int, str, str]:
    """Run REVIEW_GATE_REVIEWER_CMD. The command reads the prompt on stdin.

    shell=True is required: the operator's command is a shell string
    (for example `ollama run qwen3:14b`). It is not taken from the diff.
    """
    proc = subprocess.run(command, input=prompt, shell=True, capture_output=True,
                          text=True, timeout=timeout, errors="replace")
    out = proc.stdout or ""
    err = proc.stderr or ""
    return out, proc.returncode, err, "\n".join([out, err])


def do_review(diff_text: str, files: list[str], classification: dict,
              spec: str | None, model_override: str | None, timeout: int,
              truncated: bool, wall: _Wall) -> dict:
    model = choose_model(classification, model_override)
    prompt = build_prompt(diff_text, files, classification, spec, truncated)
    return _capture(
        "claude", model, classification, timeout,
        lambda budget: run_reviewer(prompt, model, budget),
        "claude binary not found -- allowed (fail-open)",
        wall,
    )


def do_codex_review(diff_text: str, files: list[str], classification: dict,
                    spec: str | None, codex_model: str | None, timeout: int,
                    truncated: bool, wall: _Wall) -> dict:
    prompt = build_prompt(diff_text, files, classification, spec, truncated)
    shown = codex_model or "codex"
    return _capture(
        "codex", shown, classification, timeout,
        lambda budget: run_codex_reviewer(prompt, codex_model, budget),
        "codex binary not found -- allowed (fail-open)",
        wall,
    )


def do_cmd_review(diff_text: str, files: list[str], classification: dict,
                  spec: str | None, timeout: int, truncated: bool, wall: _Wall) -> dict:
    command = os.environ.get("REVIEW_GATE_REVIEWER_CMD", "").strip()
    if not command:
        return _infra(
            "cmd", "cmd",
            "REVIEW_GATE_REVIEWER_CMD is not set -- allowed (fail-open)",
        )
    prompt = build_prompt(diff_text, files, classification, spec, truncated)
    return _capture(
        "cmd", "cmd", classification, timeout,
        lambda budget: run_cmd_reviewer(prompt, command, budget),
        "cmd reviewer not found -- allowed (fail-open)",
        wall,
    )


def run_named(name: str, diff_text: str, files: list[str], classification: dict,
              spec: str | None, model_override: str | None, codex_model: str | None,
              timeout: int, truncated: bool, wall: _Wall) -> dict:
    if name == "claude":
        return do_review(diff_text, files, classification, spec, model_override,
                         timeout, truncated, wall)
    if name == "codex":
        return do_codex_review(diff_text, files, classification, spec, codex_model,
                               timeout, truncated, wall)
    if name == "cmd":
        return do_cmd_review(diff_text, files, classification, spec, timeout,
                             truncated, wall)
    return _infra(
        name or "unknown", name or "unknown",
        f"unknown reviewer '{name}' -- allowed (fail-open)",
    )


def auto_candidates(author: str | None) -> tuple[list[str], str | None]:
    """Cross-family order, then the author's own family as a last resort.

    author claude: codex, then cmd; own family claude.
    author codex: claude, then cmd; own family codex.
    author other or unset: claude, then codex, then cmd; no own-family slot.
    """
    if author == "claude":
        return ["codex", "cmd"], "claude"
    if author == "codex":
        return ["claude", "cmd"], "codex"
    return ["claude", "codex", "cmd"], None


def _take_review(name: str, state: dict, launch) -> dict | None:
    """Run one reviewer. Any infra error (a usage limit, a timeout, a reviewer that
    cannot start) is remembered so auto can try the next reviewer."""
    verdict = launch(name)
    if verdict.get("infra_error"):
        marker = verdict.get("limit_marker")
        if marker:
            why = f"hit {marker}"
        else:
            why = "failed (" + str(verdict.get("summary", "")).split(" -- ")[0] + ")"
        state["limited"].append((name, why))
        state["last"] = verdict
        return None
    if state["limited"]:
        print(f"review-gate: {_limited_detail(state)}; ran {name}")
    return verdict


def _limited_detail(state: dict) -> str:
    return ", ".join(f"{who} {why}" for who, why in state["limited"])


def run_auto(author: str | None, diff_text: str, files: list[str], classification: dict,
             spec: str | None, model_override: str | None, codex_model: str | None,
             timeout: int, truncated: bool, wall: _Wall) -> dict:
    cross, own = auto_candidates(author)
    state: dict = {"limited": [], "last": None}

    def launch(name: str) -> dict:
        return run_named(name, diff_text, files, classification, spec,
                         model_override, codex_model, timeout, truncated, wall)

    for name in cross:
        if not reviewer_available(name):
            continue
        verdict = _take_review(name, state, launch)
        if verdict is not None:
            return verdict
    if own and reviewer_available(own):
        # Reached when no other family is installed or every one hit a limit.
        print(f"review-gate: same-family review ({own})")
        verdict = _take_review(own, state, launch)
        if verdict is not None:
            return verdict
    if state["last"] is not None:
        print(f"review-gate: {_limited_detail(state)}; no other reviewer is available")
        return state["last"]
    return launch("claude")


def combine_verdicts(parts: list[dict]) -> dict:
    """REJECT if any REJECT; else WARN if any WARN or infra error; else PASS."""
    if not parts:
        return _infra(
            "combined", "combined",
            "no reviewers listed -- allowed (fail-open)",
            panel=True, agreement=True, members=[],
        )
    values = [part.get("verdict") for part in parts]
    if any(value == "REJECT" for value in values):
        combined = "REJECT"
    elif any(value == "WARN" or part.get("infra_error") for value, part in zip(values, parts)):
        combined = "WARN"
    else:
        combined = "PASS"
    bugs: list = []
    for part in parts:
        bugs.extend(part.get("bugs") or [])
    members = [{
        "reviewer": part.get("reviewer"),
        "verdict": part.get("verdict"),
        "infra_error": bool(part.get("infra_error")),
    } for part in parts]
    return {
        "verdict": combined,
        "bugs": bugs,
        "summary": "; ".join(f"{part.get('reviewer')} {part.get('verdict')}" for part in parts),
        "model": "combined",
        "reviewer": "combined",
        "panel": True,
        "agreement": len(set(values)) == 1,
        "members": members,
        "e2e_required": any(part.get("e2e_required") for part in parts),
        "e2e_present": all(part.get("e2e_present", True) for part in parts),
    }


def _summary_label(verdict: dict) -> str:
    if verdict.get("panel"):
        return "combined"
    reviewer = verdict.get("reviewer")
    model = verdict.get("model") or "?"
    if reviewer == "claude" and model not in ("claude", "?", ""):
        return f"claude/{model}"
    if reviewer == "codex":
        if model not in ("codex", "?", ""):
            return f"codex/{model}"
        return "codex"
    if reviewer:
        return str(reviewer)
    return str(model)


def log_verdict(spine_dir: str, verdict: dict, meta: dict) -> str | None:
    """Append the verdict to the append-only spine (spine/evals.jsonl).

    Self-contained on purpose: a plain JSONL append with a typed envelope (stable id,
    one-line summary, full payload) so the gate has zero dependencies beyond the
    stdlib. Logging must never block a push."""
    try:
        os.makedirs(spine_dir, exist_ok=True)
        rnd = meta.get("round", verdict.get("round", 1))
        reviewer = meta.get("reviewer") or verdict.get("reviewer") or "review"
        stable = (
            f"{str(meta.get('head_sha', 'unknown'))[:12]}-"
            f"{reviewer}-{str(verdict.get('verdict', '?')).lower()}-r{rnd}"
        )
        payload = {**verdict, **meta}
        node = {
            "id": f"review-gate:node:review_verdict:{stable}",
            "type": "review_verdict",
            "schema_version": "0.1",
            "status": "final",
            "summary": f"{verdict.get('verdict')}: {str(verdict.get('summary', ''))[:160]}",
            "round": rnd,
            "payload": payload,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "creator": "review-gate",
        }
        if "agreement" in verdict or "agreement" in meta:
            node["agreement"] = verdict["agreement"] if "agreement" in verdict else meta["agreement"]
        with open(os.path.join(spine_dir, "evals.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(node, ensure_ascii=False) + "\n")
        return node["id"]
    except Exception as e:  # logging must never block a push
        print(f"review-gate: WARN could not log verdict to spine ({e})", file=sys.stderr)
        return None


def print_summary(verdict: dict, files: list[str]) -> None:
    v = verdict.get("verdict")
    icon = {"PASS": "PASS", "WARN": "WARN", "REJECT": "REJECT", "SKIP": "SKIP"}.get(v, v)
    print(f"\nreview-gate [{_summary_label(verdict)}] -> {icon}: {verdict.get('summary','')}")
    bugs = verdict.get("bugs") or []
    for b in bugs:
        if isinstance(b, dict):
            print(f"  - [{b.get('severity','?')}] {b.get('file','')}:{b.get('line','')} {b.get('issue','')}")
        else:
            print(f"  - {b}")
    if verdict.get("e2e_required") and not verdict.get("e2e_present", True):
        print("  ! app-facing change with no E2E evidence in the diff (WARN)")
    if v == "REJECT":
        print("\nPush BLOCKED by review-gate. Fix the findings, or override (logged): "
              "git push --no-verify")


# =====================================================================================
# CLI
# =====================================================================================

def _author_name(args) -> str | None:
    if getattr(args, "author", None):
        return args.author
    given = os.environ.get("REVIEW_GATE_AUTHOR", "").strip()
    raw = given.lower()
    if raw in ("claude", "codex", "other"):
        return raw
    if given:
        print(f"review-gate: REVIEW_GATE_AUTHOR={given!r} is not claude, codex or other; "
              "treated as unset, so the reviewer may be the author's own family")
    return None


def _codex_model_arg(args) -> str | None:
    if getattr(args, "codex_model", None):
        return args.codex_model
    raw = os.environ.get("REVIEW_GATE_CODEX_MODEL", "").strip()
    return raw or None


def _resolve_mode(args) -> tuple[str, str, list[str]]:
    """Return (panel|single, single name, panel names).

    A --reviewers flag, or REVIEW_GATE_REVIEWERS when --reviewer was not passed,
    selects panel mode. A --reviewer flag wins over both environment variables.
    """
    if args.reviewers is not None:
        names = [part.strip().lower() for part in args.reviewers.split(",") if part.strip()]
        return "panel", "", names
    if args.reviewer is not None:
        return "single", args.reviewer, []
    env_panel = os.environ.get("REVIEW_GATE_REVIEWERS", "").strip()
    env_one = os.environ.get("REVIEW_GATE_REVIEWER", "").strip().lower()
    if env_panel:
        names = [part.strip().lower() for part in env_panel.split(",") if part.strip()]
        return "panel", "", names
    if env_one in ("auto", "claude", "codex", "cmd"):
        return "single", env_one, []
    return "single", "auto", []


def _log_one(args, verdict: dict, meta: dict) -> None:
    if args.no_log:
        return
    extra = {
        **meta,
        "n_bugs": len(verdict.get("bugs") or []),
    }
    if verdict.get("reviewer"):
        extra["reviewer"] = verdict["reviewer"]
    if "agreement" in verdict:
        extra["agreement"] = verdict["agreement"]
    log_verdict(args.spine_dir, verdict, extra)


def _resolve_timeout(args, diff_text: str) -> int:
    """Explicit --timeout, clamped, or the size-scaled default when omitted.

    `is None`, not truthiness: --timeout 0 is an explicit value and clamps to 1.
    """
    if getattr(args, "timeout", None) is None:
        return default_timeout(len(diff_text.encode("utf-8", "replace")))
    return clamp_attempt_budget(args.timeout, warn=True)


def _fail_open_always(args) -> bool:
    if getattr(args, "fail_open_always", False):
        return True
    return os.environ.get("REVIEW_GATE_FAIL_OPEN_ALWAYS", "").strip() == "1"


def _review_common(args, diff_text, files, classification, truncated):
    spec = None
    if args.spec and os.path.exists(args.spec):
        with open(args.spec, encoding="utf-8", errors="replace") as handle:
            spec = handle.read()
    model_override = None if args.model == "auto" else args.model
    # One wall for every attempt and every reviewer in this run.
    wall = _Wall()
    return (diff_text, files, classification, spec, model_override,
            _codex_model_arg(args), _resolve_timeout(args, diff_text), truncated, wall)


def _finish_review(args, verdict: dict, meta: dict, files: list[str],
                   classification: dict, parts: list | None) -> int:
    code = exit_code_for(
        verdict,
        critical=bool(classification.get("critical")),
        fail_open_always=_fail_open_always(args),
        parts=parts,
    )
    _log_one(args, verdict, meta)
    print_summary(verdict, files)
    if code == 3:
        print(critical_block_line(files), file=sys.stderr)
        _log_one(args, {
            "verdict": "BLOCKED_NO_VERDICT",
            "bugs": [],
            "summary": "review could not finish on a critical path",
            "critical_files": critical_paths(files),
        }, meta)
    return code


def _review_and_report(rng_or_files, args, files, diff_text, truncated, head_sha) -> int:
    classification = classify_files(files)
    meta = {
        "head_sha": head_sha,
        "range": rng_or_files,
        "n_code_files": len(classification["code_files"]),
        "critical": classification["critical"],
        "e2e_required": classification["e2e_required"],
        "round": args.round,
    }
    if not classification["has_code"]:
        print("review-gate: docs-only change, skipped.")
        print("review-gate: non-source files: " + ", ".join(classification["doc_files"]))
        _log_one(args, {
            "verdict": "SKIP",
            "bugs": [],
            "summary": "docs-only change, skipped",
        }, meta)
        return 0
    common = _review_common(args, diff_text, files, classification, truncated)
    mode, single, names = _resolve_mode(args)
    if mode == "panel":
        parts = [run_named(name, *common) for name in names] if names else []
        for part in parts:
            _log_one(args, part, meta)
            print_summary(part, files)
        verdict = combine_verdicts(parts)
        return _finish_review(args, verdict, meta, files, classification, parts)
    if single == "auto":
        verdict = run_auto(_author_name(args), *common)
    else:
        verdict = run_named(single, *common)
    return _finish_review(args, verdict, meta, files, classification, None)


def _cap(diff_text: str) -> tuple[str, bool]:
    b = diff_text.encode("utf-8", "replace")
    if len(b) > MAX_DIFF_BYTES:
        # truncate on the BYTE budget (not char count), then drop any partial codepoint
        return b[:MAX_DIFF_BYTES].decode("utf-8", "ignore"), True
    return diff_text, False


def cmd_pre_push(args) -> int:
    lines = sys.stdin.read().splitlines()
    gitdir = git("rev-parse", "--git-dir").stdout.strip() or ".git"
    all_files: set[str] = set()
    diffs: list[str] = []
    tips: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        localsha, remotesha = parts[1], parts[3]
        if not localsha or localsha == ZERO_SHA:
            continue  # branch deletion
        tips.append(localsha)
        rng = compute_range(localsha, remotesha)
        all_files.update(changed_files_for_range(rng))
        diffs.append(diff_for_range(rng))
    if not tips:
        return 0
    tip = tips[-1]
    sentinel = os.path.join(gitdir, "review-gate-pass")
    try:
        if os.path.exists(sentinel) and open(sentinel).read().strip() == tip:
            print("review-gate: tip commit already reviewed (sentinel), skipped.")
            return 0
    except Exception:
        pass
    files = sorted(all_files)
    diff_text, truncated = _cap("\n".join(diffs))
    code = _review_and_report(f"push:{tip[:12]}", args, files, diff_text, truncated, tip)
    if code == 0:  # remember this exact tip so a retry/re-push does not re-review
        try:
            open(sentinel, "w").write(tip)
        except Exception:
            pass
    return code


def cmd_diff_mode(args) -> int:
    if args.range:
        rng = args.range
        files = changed_files_for_range(rng)
        diff_text, truncated = _cap(diff_for_range(rng))
        head = git("rev-parse", "HEAD").stdout.strip()
        return _review_and_report(rng, args, files, diff_text, truncated, head)
    # worktree (default) or staged
    if args.staged:
        files = [l for l in git("diff", "--cached", "--name-only").stdout.splitlines() if l.strip()]
        diff_text, truncated = _cap(git("diff", "--cached", "--no-color").stdout)
        label = "staged"
    else:
        files = [l for l in git("diff", "HEAD", "--name-only").stdout.splitlines() if l.strip()]
        diff_text, truncated = _cap(git("diff", "HEAD", "--no-color").stdout)
        label = "worktree"
    if not files:
        print(f"review-gate: no {label} changes to review.")
        return 0
    head = git("rev-parse", "HEAD").stdout.strip()
    return _review_and_report(label, args, files, diff_text, truncated, head)


def cmd_stats(args) -> int:
    path = os.path.join(args.spine_dir, "evals.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        print(f"review-gate: cannot read spine at {path}: {e}", file=sys.stderr)
        return 1
    verdicts = [r.get("payload", {}) for r in rows if r.get("type") == "review_verdict"]
    skipped = [v for v in verdicts if v.get("verdict") == "SKIP"]
    blocked = [v for v in verdicts if v.get("verdict") == "BLOCKED_NO_VERDICT"]
    # Each panel member is logged as its own review; the combined row would count it twice.
    # BLOCKED_NO_VERDICT is its own row and is never a REJECT.
    real = [v for v in verdicts
            if not v.get("infra_error")
            and v.get("verdict") not in ("SKIP", "BLOCKED_NO_VERDICT")
            and not v.get("panel")]
    buggy = [v for v in real if v.get("verdict") == "REJECT" or (v.get("bugs"))]
    n = len(real)
    rate = (100.0 * len(buggy) / n) if n else 0.0
    print(f"review-gate stats ({args.spine_dir})")
    print(f"  reviews logged:        {len(verdicts)}")
    print(f"  scored (non-infra):    {n}")
    print(f"  skipped (docs-only):   {len(skipped)}")
    print(f"  blocked, no verdict:   {len(blocked)}")
    print(f"  carried >=1 finding:   {len(buggy)}")
    print(f"  pre-gate finding rate: {rate:.0f}%   <- how often fresh eyes caught something")
    _print_reviewer_stats(verdicts)
    return 0


def _panel_disagreement(payload: dict) -> bool:
    """True when one family found something (REJECT or WARN) and another passed.

    Infra WARNs are not findings. REJECT versus WARN is not this count: both
    families found something.
    """
    members = payload.get("members") or []
    passed = False
    found = False
    for member in members:
        if not isinstance(member, dict) or member.get("infra_error"):
            continue
        verdict = member.get("verdict")
        if verdict == "PASS":
            passed = True
        elif verdict in ("REJECT", "WARN"):
            found = True
    return passed and found


def _print_reviewer_stats(verdicts: list[dict]) -> None:
    grouped: dict[str, list[dict]] = {}
    for verdict in verdicts:
        if verdict.get("panel") or verdict.get("verdict") in ("SKIP", "BLOCKED_NO_VERDICT"):
            continue
        name = verdict.get("reviewer")
        if not name or name == "combined":
            continue
        grouped.setdefault(str(name), []).append(verdict)
    for name in sorted(grouped):
        rows = grouped[name]
        real = [row for row in rows if not row.get("infra_error")]
        n = len(real)
        rejects = sum(1 for row in real if row.get("verdict") == "REJECT")
        buggy = [row for row in real if row.get("verdict") == "REJECT" or row.get("bugs")]
        rate = (100.0 * len(buggy) / n) if n else 0.0
        print(f"  {name}: reviews {n}, REJECT {rejects}, finding rate {rate:.0f}%")
    panels = [verdict for verdict in verdicts if verdict.get("panel")]
    if panels:
        count = sum(1 for panel in panels if _panel_disagreement(panel))
        print(f"  panel disagreements: {count}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fresh-context adversarial review gate.")
    ap.add_argument("--pre-push", nargs="*", metavar="ARG",
                    help="git pre-push hook mode (reads ref lines on stdin)")
    ap.add_argument("--range", help="explicit git range to review, e.g. origin/main..HEAD")
    ap.add_argument("--worktree", action="store_true", help="review working tree vs HEAD (default)")
    ap.add_argument("--staged", action="store_true", help="review staged changes only")
    ap.add_argument("--model", default="auto", choices=["auto", "sonnet", "opus"],
                    help="Claude model. auto = Sonnet, Opus on critical paths")
    ap.add_argument("--reviewer", default=None, choices=["auto", "claude", "codex", "cmd"],
                    help="reviewer (default auto, or $REVIEW_GATE_REVIEWER). Flag wins.")
    ap.add_argument("--author", default=None, choices=["claude", "codex", "other"],
                    help="family that wrote the change (or $REVIEW_GATE_AUTHOR)")
    ap.add_argument("--reviewers", default=None,
                    help="panel, comma-separated (or $REVIEW_GATE_REVIEWERS), e.g. claude,codex")
    ap.add_argument("--codex-model", default=None,
                    help="Codex model (or $REVIEW_GATE_CODEX_MODEL). Default: pass no model flag.")
    ap.add_argument("--round", type=int, default=1,
                    help="loop round stored on each spine record (default 1)")
    ap.add_argument("--timeout", type=int, default=None,
                    help="per-attempt budget in seconds (default: 180 plus 1 per "
                         "500 diff bytes, clamped to 180..300; explicit values "
                         "clamp to 1..600)")
    ap.add_argument("--fail-open-always", action="store_true",
                    help="allow the push even when a critical path got no usable "
                         "verdict (also REVIEW_GATE_FAIL_OPEN_ALWAYS=1)")
    ap.add_argument("--spec", help="path to an acceptance spec to check the diff against")
    ap.add_argument("--spine-dir", default=DEFAULT_SPINE_DIR)
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--stats", action="store_true", help="print logged-review stats and exit")
    args = ap.parse_args()

    if args.stats:
        return cmd_stats(args)
    if args.pre_push is not None:
        return cmd_pre_push(args)
    return cmd_diff_mode(args)


if __name__ == "__main__":
    # Fail-open on the gate's OWN errors: exit 1 must mean "reviewed and REJECTed",
    # and exit 3 must mean "no usable verdict on a critical path".
    # A bug in here must not block every push.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"review-gate: WARN internal error, allowing push (fail-open): {exc}", file=sys.stderr)
        sys.exit(0)
