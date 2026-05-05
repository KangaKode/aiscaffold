#!/usr/bin/env python3
"""
Quick validation checks -- runs directly on template files (~5 seconds).

Checks:
  1. Banned patterns (eval, exec, pickle, hardcoded secrets)
  2. File size limits (>500 lines warning, >800 lines FAIL)
  3. Jinja template syntax (parse without rendering)
  4. IP protection (no proprietary doc references)
  5. Basic Python syntax (compile check on non-Jinja .py files)

Exit code 0 = all passed, 1 = failures found.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "template", "{{project_slug}}"
)
TEMPLATE_SRC = os.path.join(TEMPLATE_DIR, "src", "{{project_slug}}")

BANNED_PATTERNS = [
    (r'\beval\s*\(', "eval() is banned -- use ast.literal_eval() or json.loads()"),
    (r'\bexec\s*\(', "exec() is banned -- use specific function calls"),
    (r'\bpickle\.loads?\s*\(', "pickle is banned -- use json for serialization"),
    (r'["\']sk-[a-zA-Z0-9]{10,}["\']', "Possible hardcoded API key (sk-...)"),
    (r'password\s*=\s*["\'][^"\']{8,}["\']', "Possible hardcoded password"),
    (r'api[_-]?key\s*=\s*["\'][a-zA-Z0-9]{10,}["\']', "Possible hardcoded API key"),
]

IP_PROTECTION_PATTERNS = [
    (r'AI_ENGINEERING_BEST_PRACTICES', "Reference to removed proprietary document"),
]

MAX_FILE_LINES_WARN = 500
MAX_FILE_LINES_FAIL = 800
SKIP_DIRS = {
    ".git",
    ".validation-venv",
    ".tmp-validation",
    ".tmp-copier-debug",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

findings = []
warnings = []

MARKDOWN_LINK_RE = re.compile(r'(?<!!)\[[^\]]+\]\(([^)]+)\)')


def check_banned_patterns(filepath, content):
    is_security_file = any(
        name in filepath
        for name in ("red_team", "prompt_guard", "validators", "security")
    )
    is_doc_or_agent = filepath.endswith((".md", ".mdc"))

    for n, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if is_doc_or_agent:
            continue
        if is_security_file and (
            "r'" in line or 'r"' in line or "PATTERNS" in line
            or "pattern" in line.lower() or "desc" in line.lower()
            or stripped.startswith('"') or stripped.startswith("'")
            or "Replace" in line or "remediation" in line.lower()
        ):
            continue
        for pattern, message in BANNED_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append(f"  FAIL: {rel(filepath)}:{n} -- {message}")
                findings.append(f"        {stripped[:100]}")


def check_ip_protection(filepath, content):
    for n, line in enumerate(content.split("\n"), 1):
        for pattern, message in IP_PROTECTION_PATTERNS:
            if re.search(pattern, line):
                findings.append(f"  FAIL: {rel(filepath)}:{n} -- {message}")
                findings.append(f"        {line.strip()[:100]}")


def check_file_size(filepath, content):
    lines = content.count("\n") + 1
    if lines > MAX_FILE_LINES_FAIL:
        findings.append(f"  FAIL: {rel(filepath)} -- {lines} lines (max {MAX_FILE_LINES_FAIL})")
    elif lines > MAX_FILE_LINES_WARN:
        warnings.append(f"  WARN: {rel(filepath)} -- {lines} lines (recommended max {MAX_FILE_LINES_WARN})")


def check_jinja_syntax(filepath, content):
    try:
        from jinja2 import Environment
        env = Environment()
        env.parse(content)
    except ImportError:
        pass
    except Exception as e:
        error_msg = str(e).split("\n")[0]
        findings.append(f"  FAIL: {rel(filepath)} -- Jinja syntax error: {error_msg}")


def check_python_syntax(filepath, content):
    if "{{" in content or "{%" in content:
        return
    try:
        compile(content, filepath, "exec")
    except SyntaxError as e:
        findings.append(f"  FAIL: {rel(filepath)}:{e.lineno} -- Python syntax error: {e.msg}")


def rel(filepath):
    return os.path.relpath(filepath, os.path.dirname(TEMPLATE_DIR))


def scan_directory(directory, extensions=(".py", ".jinja")):
    count = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not any(fname.endswith(ext) for ext in extensions):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            count += 1
            check_banned_patterns(filepath, content)
            check_ip_protection(filepath, content)
            check_file_size(filepath, content)
            if fname.endswith(".jinja"):
                check_jinja_syntax(filepath, content)
            if fname.endswith(".py"):
                check_python_syntax(filepath, content)
    return count


def check_markdown_links():
    """Validate local Markdown links so public GitHub docs do not 404."""
    repo_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    checked = 0
    for filepath in repo_root.rglob("*.md"):
        if any(part in SKIP_DIRS for part in filepath.parts):
            continue
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            for match in MARKDOWN_LINK_RE.finditer(line):
                raw_target = match.group(1).strip()
                if not raw_target or raw_target.startswith(("#", "http://", "https://", "mailto:")):
                    continue

                target = raw_target.split("#", 1)[0].strip()
                if not target:
                    continue

                target = unquote(target)
                if not (filepath.parent / target).exists():
                    findings.append(
                        f"  FAIL: {filepath.relative_to(repo_root)}:{line_no} -- broken local Markdown link: {raw_target}"
                    )
                    findings.append(f"        {line.strip()[:100]}")
                checked += 1
    return checked


def main():
    print(f"Scanning template files...")

    py_count = scan_directory(TEMPLATE_SRC, extensions=(".py",))
    jinja_count = scan_directory(TEMPLATE_DIR, extensions=(".jinja",))
    other_py = scan_directory(os.path.join(TEMPLATE_DIR, "scripts"), extensions=(".py",))
    other_py += scan_directory(os.path.join(TEMPLATE_DIR, "evals"), extensions=(".py",))
    other_py += scan_directory(os.path.join(TEMPLATE_DIR, "tests"), extensions=(".py",))

    total = py_count + jinja_count + other_py

    also_scan = scan_directory(
        os.path.join(TEMPLATE_DIR, ".cursor"), extensions=(".md", ".mdc")
    )
    markdown_links = check_markdown_links()

    for pattern, message in IP_PROTECTION_PATTERNS:
        for root, dirs, files in os.walk(TEMPLATE_DIR):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname.endswith((".md", ".mdc", ".yml", ".yaml")):
                    filepath = os.path.join(root, fname)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            content = f.read()
                        check_ip_protection(filepath, content)
                    except Exception:
                        pass

    print(f"  Scanned {total} code files + {also_scan} doc files")
    print(f"  Checked {markdown_links} local Markdown links")
    print()

    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(w)
        print()

    if findings:
        print(f"\033[31mFAILURES ({len(findings) // 2}):\033[0m")
        for f in findings:
            print(f)
        print()
        print(f"\033[31m✗ Quick checks FAILED\033[0m")
        sys.exit(1)
    else:
        print(f"\033[32m✓ {total} files checked, 0 failures, {len(warnings)} warnings\033[0m")
        sys.exit(0)


if __name__ == "__main__":
    main()
