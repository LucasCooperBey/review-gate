#!/usr/bin/env python3
"""review_gate -- fresh-context adversarial review gate for code changes.

Classifies a diff, runs a fresh-context `claude -p` adversarial reviewer (Sonnet by
default, Opus on auth/payment/migration paths), parses a structured verdict, logs it
to an append-only spine, and returns an exit code (REJECT -> 1) suitable for a git
pre-push hook.

Fresh context is the whole point: each review is a separate `claude -p` process that
sees ONLY the diff + changed-file list + (optional) acceptance spec, never the
authoring conversation. That removes the author-bias self-review failure mode.

Fail-open on infra error: if the reviewer times out or errors, the gate WARNs and
ALLOWS (exit 0) with a loud line. The review is additive safety, not the only safety.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

# -- locations ------------------------------------------------------------------
# Spine dir: $REVIEW_GATE_SPINE_DIR > a "spine/" directory next to this script.
# The spine is a plain append-only JSONL log; the envelope shape follows the
# spine pattern (github.com/Lucface/spine-pattern).
DEFAULT_SPINE_DIR = os.environ.get("REVIEW_GATE_SPINE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "spine")
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
MAX_DIFF_BYTES = 60_000
ZERO_SHA = "0" * 40

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
    out = dict(obj)
    out["verdict"] = verdict
    out["bugs"] = bugs
    out.setdefault("summary", "")
    return out


def exit_code_for(verdict: dict) -> int:
    """REJECT blocks (1); everything else (PASS/WARN) allows (0)."""
    return 1 if verdict.get("verdict") == "REJECT" else 0


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


def run_reviewer(prompt: str, model: str, timeout: int) -> tuple[str, int, str]:
    """Run a fresh-context headless review. Returns (result_text, returncode, stderr)."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", model, "--output-format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          errors="replace")
    out = proc.stdout
    # `claude --output-format json` wraps the reply: {"type":"result","result":"<text>",...}
    try:
        env = json.loads(out)
        result_text = env.get("result", out) if isinstance(env, dict) else out
    except Exception:
        result_text = out
    return result_text, proc.returncode, proc.stderr


def do_review(diff_text: str, files: list[str], classification: dict,
              spec: str | None, model_override: str | None, timeout: int,
              truncated: bool) -> dict:
    model = choose_model(classification, model_override)
    prompt = build_prompt(diff_text, files, classification, spec, truncated)
    try:
        text, rc, err = run_reviewer(prompt, model, timeout)
    except subprocess.TimeoutExpired:
        return {"verdict": "WARN", "bugs": [], "infra_error": True, "model": model,
                "summary": f"reviewer timed out after {timeout}s -- allowed (fail-open)"}
    except FileNotFoundError:
        return {"verdict": "WARN", "bugs": [], "infra_error": True, "model": model,
                "summary": "claude binary not found -- allowed (fail-open)"}
    if rc != 0 and not text.strip():
        return {"verdict": "WARN", "bugs": [], "infra_error": True, "model": model,
                "summary": f"reviewer exited {rc}: {err[:200]} -- allowed (fail-open)"}
    v = parse_verdict(text)
    v["model"] = model
    v.setdefault("e2e_required", classification["e2e_required"])
    return v


def log_verdict(spine_dir: str, verdict: dict, meta: dict) -> str | None:
    """Append the verdict to the append-only spine (spine/evals.jsonl).

    Self-contained on purpose: a plain JSONL append with a typed envelope (stable id,
    one-line summary, full payload) so the gate has zero dependencies beyond the
    stdlib. Logging must never block a push."""
    try:
        os.makedirs(spine_dir, exist_ok=True)
        stable = f"{meta.get('head_sha', 'unknown')[:12]}-{str(verdict.get('verdict', '?')).lower()}"
        node = {
            "id": f"review-gate:node:review_verdict:{stable}",
            "type": "review_verdict",
            "schema_version": "0.1",
            "status": "final",
            "summary": f"{verdict.get('verdict')}: {str(verdict.get('summary', ''))[:160]}",
            "payload": {**verdict, **meta},
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "creator": "review-gate",
        }
        with open(os.path.join(spine_dir, "evals.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(node, ensure_ascii=False) + "\n")
        return node["id"]
    except Exception as e:  # logging must never block a push
        print(f"review-gate: WARN could not log verdict to spine ({e})", file=sys.stderr)
        return None


def print_summary(verdict: dict, files: list[str]) -> None:
    v = verdict.get("verdict")
    icon = {"PASS": "PASS", "WARN": "WARN", "REJECT": "REJECT"}.get(v, v)
    print(f"\nreview-gate [{verdict.get('model','?')}] -> {icon}: {verdict.get('summary','')}")
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

def _review_and_report(rng_or_files, args, files, diff_text, truncated, head_sha) -> int:
    classification = classify_files(files)
    if not classification["has_code"]:
        print("review-gate: docs-only change, skipped.")
        return 0
    spec = None
    if args.spec and os.path.exists(args.spec):
        spec = open(args.spec, encoding="utf-8", errors="replace").read()
    verdict = do_review(diff_text, files, classification, spec,
                        None if args.model == "auto" else args.model,
                        args.timeout, truncated)
    if not args.no_log:
        log_verdict(args.spine_dir, verdict, {
            "head_sha": head_sha,
            "range": rng_or_files,
            "n_code_files": len(classification["code_files"]),
            "critical": classification["critical"],
            "e2e_required": classification["e2e_required"],
            "n_bugs": len(verdict.get("bugs") or []),
        })
    print_summary(verdict, files)
    return exit_code_for(verdict)


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
    real = [v for v in verdicts if not v.get("infra_error")]
    buggy = [v for v in real if v.get("verdict") == "REJECT" or (v.get("bugs"))]
    n = len(real)
    rate = (100.0 * len(buggy) / n) if n else 0.0
    print(f"review-gate stats ({args.spine_dir})")
    print(f"  reviews logged:        {len(verdicts)}")
    print(f"  scored (non-infra):    {n}")
    print(f"  carried >=1 finding:   {len(buggy)}")
    print(f"  pre-gate finding rate: {rate:.0f}%   <- how often fresh eyes caught something")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fresh-context adversarial review gate.")
    ap.add_argument("--pre-push", nargs="*", metavar="ARG",
                    help="git pre-push hook mode (reads ref lines on stdin)")
    ap.add_argument("--range", help="explicit git range to review, e.g. origin/main..HEAD")
    ap.add_argument("--worktree", action="store_true", help="review working tree vs HEAD (default)")
    ap.add_argument("--staged", action="store_true", help="review staged changes only")
    ap.add_argument("--model", default="auto", choices=["auto", "sonnet", "opus"])
    ap.add_argument("--timeout", type=int, default=180)
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
    # never "the gate crashed". A bug in here must not block every push.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"review-gate: WARN internal error, allowing push (fail-open): {exc}", file=sys.stderr)
        sys.exit(0)
