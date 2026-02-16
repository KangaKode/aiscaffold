#!/usr/bin/env python3
"""
Red Team Pre-Commit Check.

Runs adversarial checks on staged files before allowing a commit.
Any BLOCKING finding prevents the commit.

Run manually: python scripts/red_team_check.py [files...]
Reference: .cursor/agents/red-team.md
"""

import ast
import os
import re
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_PATTERNS = [
    (r'["\']sk-[a-zA-Z0-9_-]{10,}["\']', "Possible API key (sk-...)"),
    (r'api[_-]?key\s*=\s*["\'][^"\']{10,}["\']', "Possible hardcoded API key"),
    (r'password\s*=\s*["\'][^"\']+["\']', "Possible hardcoded password"),
    (r'token\s*=\s*["\'][a-zA-Z0-9_-]{20,}["\']', "Possible hardcoded token"),
]

SQL_INJECTION_PATTERNS = [
    (r'execute\(f["\']', "Possible SQL injection via f-string"),
    (r'execute\(["\'].*\.format\(', "Possible SQL injection via .format()"),
]

DDL_PATTERNS = [r'ALTER\s+TABLE', r'CREATE\s+(TABLE|INDEX|TRIGGER)', r'DROP\s+(TABLE|INDEX)\s+IF\s+EXISTS', r'PRAGMA']

DANGEROUS_PATTERNS = [
    (r'\beval\s*\(', "Use of eval()"),
    (r'\bexec\s*\(', "Use of exec()"),
    (r'\bpickle\.loads?\s*\(', "Use of pickle"),
]


class Finding:
    def __init__(self, severity, filepath, line, message, evidence, fix):
        self.severity = severity
        self.filepath = filepath
        self.line = line
        self.message = message
        self.evidence = evidence
        self.fix = fix

    def __str__(self):
        rel = os.path.relpath(self.filepath, PROJECT_ROOT) if self.filepath else "N/A"
        return f"[{self.severity}] {rel}:{self.line} - {self.message}\n  EVIDENCE: {self.evidence}\n  FIX: {self.fix}"


def check_secrets(fp, content):
    findings = []
    for n, line in enumerate(content.split("\n"), 1):
        if line.strip().startswith("#"):
            continue
        for pat, desc in SECRET_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                findings.append(Finding("BLOCKING", fp, n, f"Security: {desc}", line.strip()[:100],
                    "Replace the hardcoded value with os.environ.get('ENV_VAR_NAME'). "
                    "Add the variable to .env.example with a placeholder value. "
                    "Never commit secrets to git."))
    return findings


def check_sql_injection(fp, content):
    findings = []
    for n, line in enumerate(content.split("\n"), 1):
        for pat, desc in SQL_INJECTION_PATTERNS:
            if re.search(pat, line):
                is_ddl = any(re.search(d, line, re.IGNORECASE) for d in DDL_PATTERNS)
                sev = "WARNING" if is_ddl else "BLOCKING"
                findings.append(Finding(sev, fp, n, f"Security: {desc}", line.strip()[:100],
                    "DDL statements are acceptable if input is trusted." if is_ddl else
                    "Replace f-string/format with parameterized query: "
                    "conn.execute('SELECT * FROM t WHERE id = ?', (user_id,)). "
                    "Never interpolate user input into SQL strings."))
    return findings


def check_dangerous(fp, content):
    findings = []
    for n, line in enumerate(content.split("\n"), 1):
        if line.strip().startswith("#"):
            continue
        for pat, desc in DANGEROUS_PATTERNS:
            if re.search(pat, line):
                fixes = {
                    r'\beval\s*\(': "Replace eval() with ast.literal_eval() for data parsing, "
                                    "or json.loads() for JSON. Never evaluate untrusted strings.",
                    r'\bexec\s*\(': "Replace exec() with a specific function call or dispatch table. "
                                    "exec() allows arbitrary code execution from untrusted input.",
                    r'\bpickle\.loads?\s*\(': "Replace pickle with json.loads()/json.dumps() for serialization. "
                                              "Pickle can execute arbitrary code during deserialization.",
                }
                fix = next((v for k, v in fixes.items() if re.search(k, line)), "Use safe alternatives.")
                findings.append(Finding("BLOCKING", fp, n, f"Security: {desc}", line.strip()[:100], fix))
    return findings


def check_architecture(fp, content):
    # Import FORBIDDEN_IMPORTS from test_architecture if available
    findings = []
    rel = os.path.relpath(fp, PROJECT_ROOT)
    module = rel.replace("\\", "/").split("/")[0]

    # Minimal built-in rules -- extend by editing this dict
    forbidden = {}
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests"))
        from test_architecture import FORBIDDEN_IMPORTS
        forbidden = FORBIDDEN_IMPORTS
    except Exception:
        pass

    if module not in forbidden:
        return findings

    try:
        tree = ast.parse(content, filename=fp)
    except SyntaxError:
        return findings

    for node in tree.body:
        name = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            name = node.module
        if name:
            for prefix in forbidden.get(module, []):
                if name.startswith(prefix):
                    findings.append(Finding("BLOCKING", fp, node.lineno,
                        f"Architecture: {module}/ imports {prefix.rstrip('.')}/ (forbidden)",
                        f"import {name}", "Extract shared types to a lower layer"))
    return findings


def check_file_size(fp, content):
    lc = content.count("\n") + 1
    if lc > 500:
        return [Finding("WARNING", fp, 0, f"Size: {lc} lines (limit: 500)", f"{lc} lines", "Split into smaller modules")]
    return []


def check_data_safety(fp, content):
    findings = []
    for n, line in enumerate(content.split("\n"), 1):
        if re.search(r'DROP\s+TABLE\s+(?!IF\s+EXISTS)', line, re.IGNORECASE):
            findings.append(Finding("BLOCKING", fp, n, "Data: DROP TABLE without IF EXISTS", line.strip()[:100], "Use DROP TABLE IF EXISTS"))
        if re.search(r'DELETE\s+FROM\s+\w+\s*["\';]', line, re.IGNORECASE) and "WHERE" not in line.upper():
            findings.append(Finding("BLOCKING", fp, n, "Data: DELETE without WHERE", line.strip()[:100], "Add WHERE clause"))
    return findings


def run_checks(files):
    all_f = []
    for fp in files:
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        is_test = "/tests/" in fp
        all_f.extend(check_secrets(fp, content))
        all_f.extend(check_architecture(fp, content))
        all_f.extend(check_file_size(fp, content))
        if not is_test:
            all_f.extend(check_sql_injection(fp, content))
            all_f.extend(check_dangerous(fp, content))
            all_f.extend(check_data_safety(fp, content))
    return all_f


def main():
    if len(sys.argv) > 1:
        files = [os.path.join(PROJECT_ROOT, f) if not os.path.isabs(f) else f for f in sys.argv[1:]]
    else:
        try:
            r = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                capture_output=True, text=True, cwd=PROJECT_ROOT)
            files = [os.path.join(PROJECT_ROOT, f) for f in r.stdout.strip().split("\n") if f.endswith(".py") and f]
        except Exception:
            files = []

    if not files:
        print("Red Team: No files to check.")
        sys.exit(0)

    print(f"Red Team: Scanning {len(files)} file(s)...")
    findings = run_checks(files)
    blocking = [f for f in findings if f.severity == "BLOCKING"]
    warnings = [f for f in findings if f.severity == "WARNING"]

    if blocking:
        print(f"\n{'='*60}\nRED TEAM: {len(blocking)} BLOCKING - COMMIT BLOCKED\n{'='*60}\n")
        for f in blocking:
            print(f)
            print()
    if warnings:
        print(f"\n{'='*60}\nRED TEAM: {len(warnings)} WARNING(S)\n{'='*60}\n")
        for f in warnings:
            print(f)
            print()
    if not blocking and not warnings:
        print("Red Team: PASS")

    sys.exit(1 if blocking else 0)


if __name__ == "__main__":
    main()
