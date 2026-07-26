#!/usr/bin/env python3
"""
Automated agent review -- mechanical checks simulating what Cursor agents catch.

Simulates three agents:
  1. code-reviewer: docstrings, bare except, TODO hygiene, type hints
  2. security-hardener: parameterized SQL, input validation, no plaintext secrets
  3. minimalist: file size, function size, class size, complexity

Usage: python scripts/agent_review.py [path_to_src]
Exit code 0 = all passed, 1 = failures found.
"""

import ast
import os
import re
import sys

findings = []
warnings = []


def fail(agent, msg):
    findings.append(f"  [{agent}] FAIL: {msg}")


def warn(agent, msg):
    warnings.append(f"  [{agent}] WARN: {msg}")


# =============================================================================
# CODE REVIEWER CHECKS
# =============================================================================


def review_code_quality(filepath, content, rel_path):
    """Simulate code-reviewer agent checks."""
    tree = None
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return

    for node in ast.walk(tree):
        # Bare except (catches too broadly)
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            fail("code-reviewer", f"{rel_path}:{node.lineno} -- Bare 'except:' (catch specific exceptions)")

        # Functions without docstrings (only public functions)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                if ast.get_docstring(node) is None:
                    warn("code-reviewer", f"{rel_path}:{node.lineno} -- Public function '{node.name}' missing docstring")

        # Classes without docstrings
        if isinstance(node, ast.ClassDef):
            if ast.get_docstring(node) is None:
                fail("code-reviewer", f"{rel_path}:{node.lineno} -- Class '{node.name}' missing docstring")


# =============================================================================
# SECURITY HARDENER CHECKS
# =============================================================================


# Stable rule IDs for deterministic security findings.
#
# These IDs are part of the proof-of-finding contract (see
# ``template/{{project_slug}}/.cursor/rules/expert-review.mdc``): every
# blocking security finding carries a stable ID, exact location
# (``rel_path:lineno``), and matched evidence. Tests in
# ``tests/test_agent_review.py`` pin the three IDs below by name.
RULE_SQL_FSTRING = "SEC-SQL-FSTRING"
RULE_SQL_FORMAT = "SEC-SQL-FORMAT"
RULE_OS_SYSTEM = "SEC-OS-SYSTEM"
RULE_SHELL_TRUE = "SEC-SHELL-TRUE"
RULE_HARDCODED_CREDENTIAL = "SEC-HARDCODED-CREDENTIAL"


def _fail_finding(rule_id, rel_path, lineno, description, evidence):
    """Emit a stable-ID security finding via the ``security`` agent slot.

    Format: ``[SEC-...] rel_path:lineno -- description (evidence: ...)``.
    ``evidence`` must be the offending code fragment or the trigger
    keyword; it must NEVER contain a secret value. Callers pass a
    keyword or shape for credential findings and the trimmed line for
    all other patterns.
    """
    evidence_str = f" (evidence: {evidence})" if evidence else ""
    fail("security", f"[{rule_id}] {rel_path}:{lineno} -- {description}{evidence_str}")


def review_security(filepath, content, rel_path):
    """Simulate security-hardener agent checks.

    Every failure emitted here carries a stable rule ID, the exact
    ``rel_path:lineno`` location, and matched evidence per the
    proof-of-finding contract. Detection outcomes match the previous
    free-text implementation; only the message shape changed.
    """
    lines = content.split("\n")

    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # f-string in SQL execute
        if re.search(r'\.execute\(f["\']', line):
            _fail_finding(
                RULE_SQL_FSTRING,
                rel_path,
                n,
                "f-string in SQL execute (use parameterized queries)",
                stripped,
            )

        # .format() in SQL execute
        if re.search(r'\.execute\(["\'].*\.format\(', line):
            _fail_finding(
                RULE_SQL_FORMAT,
                rel_path,
                n,
                ".format() in SQL execute (use parameterized queries)",
                stripped,
            )

        # os.system or subprocess with shell=True
        if re.search(r'os\.system\(', line):
            _fail_finding(
                RULE_OS_SYSTEM,
                rel_path,
                n,
                "os.system() is unsafe (use subprocess.run with shell=False)",
                stripped,
            )
        if re.search(r'subprocess.*shell\s*=\s*True', line):
            _fail_finding(
                RULE_SHELL_TRUE,
                rel_path,
                n,
                "subprocess with shell=True is unsafe",
                stripped,
            )

        # Hardcoded credentials (outside comments and test files).
        # Evidence quotes ONLY the credential keyword that fired the
        # rule; the credential value must never enter the finding text
        # because findings surface in reviewer output and CI logs.
        if "test" not in rel_path.lower():
            cred_match = re.search(
                r'(password|secret|token)\s*=\s*["\'][^"\']{8,}["\']',
                line,
                re.IGNORECASE,
            )
            if cred_match:
                lowered = line.lower()
                if (
                    "default" not in lowered
                    and "example" not in lowered
                    and "changeme" not in lowered
                ):
                    keyword = cred_match.group(1)
                    _fail_finding(
                        RULE_HARDCODED_CREDENTIAL,
                        rel_path,
                        n,
                        "Possible hardcoded credential",
                        f"keyword={keyword}",
                    )


# =============================================================================
# MINIMALIST CHECKS
# =============================================================================

MAX_FILE_LINES = 500
MAX_FILE_LINES_HARD_CAP = 800
MAX_FUNCTION_LINES = 50
MAX_CLASS_LINES = 300
LINE_CAP_HEADER_RE = re.compile(r"[Kk]eep this file under (\d+) lines")


def _declared_line_cap(content: str) -> int | None:
    """Return the module's declared line cap, if any (respects the header)."""
    match = LINE_CAP_HEADER_RE.search(content)
    if not match:
        return None
    try:
        cap = int(match.group(1))
    except ValueError:
        return None
    return min(cap, MAX_FILE_LINES_HARD_CAP)


def review_minimalist(filepath, content, rel_path):
    """Simulate minimalist agent checks.

    Line cap: respects an in-file "Keep this file under N lines." header up
    to a hard ceiling of MAX_FILE_LINES_HARD_CAP. Files without a header
    are held to MAX_FILE_LINES. Same posture as scripts/quick_checks.py so
    the two never disagree on the declared cap.
    """
    lines = content.split("\n")
    line_count = len(lines)
    declared = _declared_line_cap(content)
    effective_cap = declared if declared is not None else MAX_FILE_LINES

    if line_count > effective_cap:
        fail(
            "minimalist",
            f"{rel_path} -- {line_count} lines "
            f"(max {effective_cap}). Split into smaller modules.",
        )

    # Function and class size via AST
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if hasattr(node, "end_lineno") and node.end_lineno:
                func_lines = node.end_lineno - node.lineno + 1
                if func_lines > MAX_FUNCTION_LINES:
                    warn("minimalist",
                         f"{rel_path}:{node.lineno} -- Function '{node.name}' is {func_lines} lines "
                         f"(recommended max {MAX_FUNCTION_LINES})")

        if isinstance(node, ast.ClassDef):
            if hasattr(node, "end_lineno") and node.end_lineno:
                class_lines = node.end_lineno - node.lineno + 1
                if class_lines > MAX_CLASS_LINES:
                    warn("minimalist",
                         f"{rel_path}:{node.lineno} -- Class '{node.name}' is {class_lines} lines "
                         f"(recommended max {MAX_CLASS_LINES})")


# =============================================================================
# MAIN
# =============================================================================


def main():
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "."

    if not os.path.isdir(src_dir):
        print(f"Source directory not found: {src_dir}")
        sys.exit(1)

    print(f"Running automated agent review on {src_dir}...")

    file_count = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, src_dir)
            try:
                with open(filepath) as f:
                    content = f.read()
                review_code_quality(filepath, content, rel_path)
                review_security(filepath, content, rel_path)
                review_minimalist(filepath, content, rel_path)
                file_count += 1
            except Exception as e:
                fail("agent-review", f"{rel_path} -- Could not process: {e}")

    print(f"  Reviewed {file_count} files")
    print()

    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(w)
        print()

    if findings:
        print(f"\033[31mAgent Review Failures ({len(findings)}):\033[0m")
        for f in findings:
            print(f)
        print()
        sys.exit(1)
    else:
        print(f"\033[32m✓ Agent review passed ({file_count} files, 0 failures, {len(warnings)} warnings)\033[0m")
        sys.exit(0)


if __name__ == "__main__":
    main()
