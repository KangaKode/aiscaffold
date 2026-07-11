#!/usr/bin/env bash
# validate_generated.sh -- Generate test projects and run the full validation suite.
#
# Usage: bash scripts/validate_generated.sh [project_type] [llm_provider] [persistence] [profile]
#   Default: web-app anthropic sqlite all
#
# Profiles (generation configs):
#   full        -- every toggle on (include_evals/api_gateway/deployment/learning=true)
#   gateway-off -- include_api_gateway=false, everything else on: the project
#                  must generate WITHOUT the api/ tree, import cleanly, and
#                  pass its reduced test suite
#   minimal     -- include_deployment=false + include_evals=false: generation
#                  and exclusion checks only (proves the toggles' off state
#                  actually removes Dockerfile/docker-compose/deploy/evals)
#   defaults    -- pure copier defaults, no --data beyond project_name: what a
#                  real user gets when accepting every default
#   all         -- run all four profiles in sequence (default)
#
# Steps (per profile):
#   0.   Quick checks on templates (no generation; full profile only)
#   1.   Generate a test project via copier (non-interactive)
#   1.5  Unrendered template check (no leftover jinja in generated .py)
#   2.   Run ruff (linting)
#   3.   Run bandit (security)
#   4.   Python syntax check on all generated modules
#   5.   Run red team checks
#   6.   Run AI-specific checks
#   7.   Run automated agent review
#   8.   Unit tests (pytest on the generated project) + import sweep
#        (skipped in the generation-only 'minimal' profile)
#   9.   Injection-defense golden set
#   10.  File structure check
#   11.  Toggle wiring check (per-profile inclusion/exclusion assertions)
#
# Exit code 0 = all passed, non-zero = failures found.

set -uo pipefail

PROJECT_TYPE="${1:-web-app}"
LLM_PROVIDER="${2:-anthropic}"
PERSISTENCE="${3:-sqlite}"
PROFILE="${4:-all}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Profile dispatcher: "all" re-invokes this script once per profile.
# ---------------------------------------------------------------------------
if [ "$PROFILE" = "all" ]; then
    OVERALL=0
    SUMMARY=""
    for p in full gateway-off minimal defaults; do
        echo ""
        echo "==================================================================="
        echo "  PROFILE: $p"
        echo "==================================================================="
        if bash "${BASH_SOURCE[0]}" "$PROJECT_TYPE" "$LLM_PROVIDER" "$PERSISTENCE" "$p"; then
            SUMMARY="$SUMMARY
  $p: PASSED"
        else
            SUMMARY="$SUMMARY
  $p: FAILED"
            OVERALL=1
        fi
    done
    echo ""
    echo "==========================================="
    echo "  PROFILE SUMMARY:$SUMMARY"
    echo "==========================================="
    exit $OVERALL
fi

# Per-profile toggle values. GENERATION_ONLY skips the unit-test step;
# PURE_DEFAULTS generates with no --data beyond the project name.
case "$PROFILE" in
    full)
        INCLUDE_API_GATEWAY=true;  INCLUDE_DEPLOYMENT=true
        INCLUDE_EVALS=true;        INCLUDE_LEARNING=true
        PURE_DEFAULTS=0; GENERATION_ONLY=0 ;;
    gateway-off)
        INCLUDE_API_GATEWAY=false; INCLUDE_DEPLOYMENT=true
        INCLUDE_EVALS=true;        INCLUDE_LEARNING=true
        PURE_DEFAULTS=0; GENERATION_ONLY=0 ;;
    minimal)
        INCLUDE_API_GATEWAY=true;  INCLUDE_DEPLOYMENT=false
        INCLUDE_EVALS=false;       INCLUDE_LEARNING=false
        PURE_DEFAULTS=0; GENERATION_ONLY=1 ;;
    defaults)
        # Mirrors copier.yml defaults; used by the checks below, but NOT
        # passed to copier -- generation uses --defaults only.
        INCLUDE_API_GATEWAY=true;  INCLUDE_DEPLOYMENT=true
        INCLUDE_EVALS=true;        INCLUDE_LEARNING=false
        PURE_DEFAULTS=1; GENERATION_ONLY=0
        PROJECT_TYPE="web-app"; LLM_PROVIDER="anthropic"; PERSISTENCE="sqlite" ;;
    *)
        echo "Unknown profile: $PROFILE (expected full|gateway-off|minimal|defaults|all)"
        exit 2 ;;
esac

TEST_DIR="$REPO_ROOT/.tmp-validation/aiscaffold_test_$(date +%s)_$$"
PROJECT_NAME="test_project"
PROJECT_SLUG="test_project"

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { echo "  ✓ $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  ✗ $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn() { echo "  ⚠ $1"; WARN_COUNT=$((WARN_COUNT + 1)); }
section() { echo ""; echo "--- $1 ---"; }

cleanup() {
    if [ -d "$TEST_DIR" ]; then
        rm -rf "$TEST_DIR"
    fi
}
trap cleanup EXIT

# =========================================================================
# Step 0: Quick checks on templates (no generation needed)
# =========================================================================
# Template-level checks are generation-independent, so run them once (in
# the 'full' profile) instead of once per profile.
if [ "$PROFILE" = "full" ]; then
    section "Step 0: Quick Checks (templates)"
    if python3 "$SCRIPT_DIR/quick_checks.py"; then
        pass "Quick checks"
    else
        fail "Quick checks -- see output above"
    fi
fi

# =========================================================================
# Step 1: Generate test project
# =========================================================================
section "Step 1: Generate Test Project ($PROJECT_TYPE / $LLM_PROVIDER / $PERSISTENCE / profile=$PROFILE)"
mkdir -p "$TEST_DIR"

if command -v copier &>/dev/null; then
    COPIER_SOURCE="$TEST_DIR/template_source"
    python3 - "$REPO_ROOT" "$COPIER_SOURCE" <<'PY'
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])

ignored = {
    ".git",
    ".cursor",
    ".validation-venv",
    ".tmp-copier-debug",
    ".tmp-validation",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

shutil.copytree(
    source,
    destination,
    ignore=lambda _dir, names: [name for name in names if name in ignored],
)
PY

    # Determine layers based on project type (mirrors copier.yml default)
    case "$PROJECT_TYPE" in
        web-app)     LAYERS="data,analysis,components" ;;
        cli-tool)    LAYERS="data,logic" ;;
        multi-agent) LAYERS="data,analysis,orchestration,specialists,prompts" ;;
        api-service) LAYERS="data,service,routes" ;;
        *)           LAYERS="data,analysis,components" ;;
    esac

    COPIER_LOG="$TEST_DIR/copier.log"
    if [ "$PURE_DEFAULTS" = "1" ]; then
        # Pure-defaults generation: what a real user gets when accepting
        # every default. Only the required project name is provided.
        (cd "$TEST_DIR" && copier copy "$COPIER_SOURCE" "$TEST_DIR/$PROJECT_SLUG" --trust --defaults \
            --data project_name="$PROJECT_NAME" \
            >"$COPIER_LOG" 2>&1)
    else
        (cd "$TEST_DIR" && copier copy "$COPIER_SOURCE" "$TEST_DIR/$PROJECT_SLUG" --trust --defaults \
            --data project_name="$PROJECT_NAME" \
            --data project_slug="$PROJECT_SLUG" \
            --data project_description="Validation test project" \
            --data author_name="CI" \
            --data project_type="$PROJECT_TYPE" \
            --data layers="$LAYERS" \
            --data llm_provider="$LLM_PROVIDER" \
            --data persistence="$PERSISTENCE" \
            --data python_version="3.13" \
            --data include_evals="$INCLUDE_EVALS" \
            --data include_api_gateway="$INCLUDE_API_GATEWAY" \
            --data include_deployment="$INCLUDE_DEPLOYMENT" \
            --data include_learning="$INCLUDE_LEARNING" \
            >"$COPIER_LOG" 2>&1)
    fi
    COPIER_STATUS=$?

    if [ "$COPIER_STATUS" -ne 0 ] && [ "${ALLOW_COPIER_FALLBACK:-}" = "1" ]; then
        warn "copier failed; using direct Jinja render fallback for sandbox validation"
        python3 - "$REPO_ROOT/template/{{project_slug}}" "$TEST_DIR/$PROJECT_SLUG" \
            "$PROJECT_NAME" "$PROJECT_SLUG" "$PROJECT_TYPE" "$LAYERS" "$LLM_PROVIDER" "$PERSISTENCE" \
            "$INCLUDE_EVALS" "$INCLUDE_API_GATEWAY" "$INCLUDE_DEPLOYMENT" "$INCLUDE_LEARNING" <<'PY'
import sys
from pathlib import Path

from jinja2 import Environment, StrictUndefined

template_root = Path(sys.argv[1])
destination_root = Path(sys.argv[2])

def _flag(value):
    return value.strip().lower() == "true"

context = {
    "project_name": sys.argv[3],
    "project_slug": sys.argv[4],
    "project_description": "Validation test project",
    "author_name": "CI",
    "project_type": sys.argv[5],
    "layers": sys.argv[6],
    "llm_provider": sys.argv[7],
    "persistence": sys.argv[8],
    "learning_backend": "sqlite",
    "python_version": "3.13",
    "include_evals": _flag(sys.argv[9]),
    "include_api_gateway": _flag(sys.argv[10]),
    "include_deployment": _flag(sys.argv[11]),
    "include_learning": _flag(sys.argv[12]),
}

env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)


def _excluded(relative: Path) -> bool:
    """Mirror the conditional _exclude globs in copier.yml."""
    parts = relative.parts
    if not context["include_evals"] and "evals" in parts:
        return True
    if not context["include_api_gateway"]:
        if "src" in parts and "api" in parts:
            return True
        # HTTP-only artifacts: k8s Service, Locust harness + compose override.
        if "deploy" in parts and relative.name.startswith("service.yaml"):
            return True
        if relative.name.startswith("docker-compose.load"):
            return True
        if "tests" in parts and "load" in parts:
            return True
    if not context["include_deployment"]:
        name = relative.name
        if name in ("Dockerfile", "Dockerfile.jinja", ".dockerignore") or "deploy" in parts:
            return True
        if name.startswith("docker-compose"):
            return True
    return False


for source in template_root.rglob("*"):
    relative = source.relative_to(template_root)
    if ".cursor" in relative.parts:
        continue
    if _excluded(relative):
        continue
    if "_copier_conf" in source.name:
        # The answers-file template needs copier's own runtime context;
        # a stub is written below instead.
        continue
    rendered_parts = [env.from_string(part).render(context) for part in relative.parts]
    output = destination_root / Path(*rendered_parts)
    if output.name.endswith(".jinja"):
        output = output.with_name(output.name.removesuffix(".jinja"))

    if source.is_dir():
        output.mkdir(parents=True, exist_ok=True)
        continue

    try:
        source_text = source.read_text()
    except UnicodeDecodeError:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(source.read_bytes())
        continue

    rendered = env.from_string(source_text).render(context) if source.name.endswith(".jinja") else source_text

    if source.name.endswith(".jinja") and rendered.strip() == "":
        continue

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)

# The destination directory is the project root (see _subdirectory in
# copier.yml).
generated_project_root = destination_root

# Stub answers file (real content requires copier's runtime context).
(generated_project_root / ".copier-answers.yml").write_text(
    "# Stub written by the sandbox fallback renderer (copier unavailable)\n"
)

for layer in context["layers"].split(","):
    layer_dir = generated_project_root / layer.strip()
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / "__init__.py").touch()

if context["include_evals"]:
    for eval_dir in ["evals/capability", "evals/regression", "evals/graders", "evals/results"]:
        (generated_project_root / eval_dir).mkdir(parents=True, exist_ok=True)
    (generated_project_root / "evals" / "__init__.py").touch()
    (generated_project_root / "evals" / "graders" / "__init__.py").touch()
PY
    fi
    
    # The copier destination IS the project root (see _subdirectory in
    # copier.yml): TEST_DIR/PROJECT_SLUG/
    GENERATED_DIR="$TEST_DIR/$PROJECT_SLUG"
    if [ ! -d "$GENERATED_DIR/src/$PROJECT_SLUG" ]; then
        # Legacy nested layout (older template revisions)
        GENERATED_DIR="$TEST_DIR/$PROJECT_SLUG/$PROJECT_SLUG"
    fi

    if [ -d "$GENERATED_DIR/src/$PROJECT_SLUG" ]; then
        pass "Project generated at $GENERATED_DIR"
        # Cache-leak check must run BEFORE ruff/pytest execute inside the
        # project (they create their own caches at runtime).
        if [ ! -d "$GENERATED_DIR/.ruff_cache" ] && [ ! -d "$GENERATED_DIR/.pytest_cache" ]; then
            pass "no cache directories generated into the project"
        else
            fail "cache directories (.ruff_cache/.pytest_cache) generated into the project"
        fi
    else
        fail "Project generation failed -- src/ directory not found"
        if [ -s "$COPIER_LOG" ]; then
            echo "Copier output:"
            sed -n '1,80p' "$COPIER_LOG"
        fi
        echo "Contents of $TEST_DIR:"
        find "$TEST_DIR" -maxdepth 3 -type d 2>/dev/null
        exit 1
    fi
else
    warn "copier not installed -- skipping generation, running checks on template directly"
    GENERATED_DIR="$TEST_DIR/$PROJECT_SLUG"
    mkdir -p "$GENERATED_DIR/src/$PROJECT_SLUG"
    cp -r "$REPO_ROOT/template/{{project_slug}}/src/{{project_slug}}/"* "$GENERATED_DIR/src/$PROJECT_SLUG/" 2>/dev/null || true
    cp -r "$REPO_ROOT/template/{{project_slug}}/scripts" "$GENERATED_DIR/scripts" 2>/dev/null || true
    cp -r "$REPO_ROOT/template/{{project_slug}}/tests" "$GENERATED_DIR/tests" 2>/dev/null || true
fi

GEN_SRC="$GENERATED_DIR/src/$PROJECT_SLUG"
GEN_ROOT="$GENERATED_DIR"

# =========================================================================
# Step 1.5: Unrendered Template Check
# =========================================================================
# A template file with {{ ... }} placeholders but no .jinja suffix ships
# UNRENDERED into generated projects (broken imports, pytest collection
# errors). Scan every generated .py file for leftover copier variables and
# jinja block tags. Docs (.md) are excluded: they legitimately discuss
# jinja syntax. Literal doubled braces in Python (f-strings, regexes like
# {{3}}) are NOT flagged -- only known copier variable names and jinja
# keywords count.
section "Step 1.5: Unrendered Template Check"
# Known limits of this checker:
#   - Scans .py files only; other rendered file types are not checked.
#   - A doubled-brace f-string whose literal text contains a copier variable
#     name (e.g. f"{{ project_slug }}") would false-positive. Acceptable:
#     rename that template to .jinja or rework the string.
#   - New copier variables are covered automatically -- names are parsed
#     from copier.yml at run time, not hardcoded here.
UNRENDERED=$(python3 - "$REPO_ROOT/copier.yml" "$GENERATED_DIR" <<'PY'
import re
import sys
from pathlib import Path

copier_yml, generated_root = Path(sys.argv[1]), Path(sys.argv[2])

# Top-level keys in copier.yml are the template variables (plus _settings).
variables = re.findall(r"^([a-z][a-z0-9_]*):", copier_yml.read_text(), re.MULTILINE)
var_re = re.compile(r"\{\{-?\s*(" + "|".join(variables) + r")\b")
block_re = re.compile(
    r"\{%-?\s*(if|elif|else|endif|for|endfor|set|include|macro|endmacro"
    r"|block|endblock|raw|endraw)\b"
)

for path in sorted(generated_root.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for line_no, line in enumerate(text.splitlines(), 1):
        if var_re.search(line) or block_re.search(line):
            print(f"{path.relative_to(generated_root)}:{line_no}: {line.strip()[:100]}")
PY
)
UNRENDERED_STATUS=$?
if [ "$UNRENDERED_STATUS" -ne 0 ]; then
    # A crashed checker is a checker error, not a clean scan -- never pass.
    fail "Unrendered-template checker errored (exit $UNRENDERED_STATUS) -- fix the checker before trusting this step"
    [ -n "$UNRENDERED" ] && echo "$UNRENDERED" | sed 's/^/    /'
elif [ -z "$UNRENDERED" ]; then
    pass "No unrendered jinja placeholders in generated .py files"
else
    fail "Unrendered jinja placeholders found (missing .jinja suffix on template?):"
    echo "$UNRENDERED" | sed 's/^/    /'
fi

# =========================================================================
# Step 2: Ruff Linting
# =========================================================================
section "Step 2: Ruff Linting"
if command -v ruff &>/dev/null; then
    if ruff check "$GEN_SRC" --quiet 2>/dev/null; then
        pass "ruff: no lint errors"
    else
        LINT_COUNT=$(ruff check "$GEN_SRC" --statistics 2>/dev/null | wc -l | tr -d ' ')
        fail "ruff: $LINT_COUNT issue categories found"
        ruff check "$GEN_SRC" --statistics 2>/dev/null | head -10
    fi
else
    warn "ruff not installed -- skipping lint"
fi

# =========================================================================
# Step 3: Bandit Security Scan
# =========================================================================
section "Step 3: Bandit Security Scan"
if command -v bandit &>/dev/null; then
    BANDIT_OUT=$(bandit -r "$GEN_SRC" -ll --quiet 2>/dev/null)
    if [ -z "$BANDIT_OUT" ]; then
        pass "bandit: no medium+ severity issues"
    else
        BANDIT_COUNT=$(echo "$BANDIT_OUT" | grep -c "Issue:" 2>/dev/null || echo "0")
        fail "bandit: $BANDIT_COUNT issues found"
        echo "$BANDIT_OUT" | head -20
    fi
else
    warn "bandit not installed -- skipping security scan"
fi

# =========================================================================
# Step 4: Python Import Check (all modules importable)
# =========================================================================
section "Step 4: Import Validation"
IMPORT_ERRORS=0
for pyfile in $(find "$GEN_SRC" -name "*.py" -not -name "__init__.py" -not -path "*/__pycache__/*"); do
    if ! python3 -c "import ast; ast.parse(open('$pyfile').read())" 2>/dev/null; then
        fail "Syntax error in $(basename $pyfile)"
        IMPORT_ERRORS=$((IMPORT_ERRORS + 1))
    fi
done
if [ "$IMPORT_ERRORS" -eq 0 ]; then
    PY_COUNT=$(find "$GEN_SRC" -name "*.py" -not -path "*/__pycache__/*" | wc -l | tr -d ' ')
    pass "All $PY_COUNT Python files parse successfully"
fi

# =========================================================================
# Step 5: Red Team Check
# =========================================================================
section "Step 5: Red Team Security Check"
RED_TEAM_SCRIPT="$GEN_ROOT/scripts/red_team_check.py"
if [ -f "$RED_TEAM_SCRIPT" ]; then
    PY_FILES=$(find "$GEN_SRC" -name "*.py" -not -path "*/__pycache__/*")
    if python3 "$RED_TEAM_SCRIPT" $PY_FILES 2>/dev/null; then
        pass "Red team: no blocking findings"
    else
        fail "Red team: blocking findings detected"
    fi
else
    warn "Red team script not found in generated project"
fi

# =========================================================================
# Step 6: AI-Specific Checks
# =========================================================================
section "Step 6: AI-Specific Checks"
if python3 "$SCRIPT_DIR/ai_checks.py" "$GEN_SRC" 2>/dev/null; then
    pass "AI checks passed"
else
    fail "AI checks: issues detected"
fi

# =========================================================================
# Step 7: Automated Agent Review
# =========================================================================
section "Step 7: Automated Agent Review"
if python3 "$SCRIPT_DIR/agent_review.py" "$GEN_SRC" 2>/dev/null; then
    pass "Agent review passed"
else
    fail "Agent review: issues detected"
fi

# =========================================================================
# Step 8: Unit Tests (run pytest on generated project) + import sweep
# =========================================================================
section "Step 8: Unit Tests"
if [ "$GENERATION_ONLY" = "1" ]; then
    echo "  (skipped: generation-only profile '$PROFILE' proves file exclusions;"
    echo "   the full/gateway-off/defaults profiles run the test suite)"
elif [ -d "$GEN_ROOT/tests" ]; then
    cd "$GEN_ROOT"
    # Install project dependencies quietly (skip LLM providers to avoid API key issues)
    pip install -q pytest pytest-asyncio pytest-cov fastapi uvicorn httpx pydantic python-dotenv 2>/dev/null
    if [ -f "pyproject.toml" ]; then
        pip install -q -e . 2>/dev/null || warn "Editable install failed -- tests may rely on repo-root imports"
    fi

    # Run all test files (all use mocks/in-process testing, no external deps)
    UNIT_FILES=""
    for f in tests/test_security.py tests/test_injection_defense.py tests/test_ingest_scan.py tests/test_llm.py tests/test_single_shot.py tests/test_learning.py tests/test_learning_maturity.py tests/test_learning_store.py tests/test_store_correctness.py tests/test_async_hardening.py tests/test_learning_wiring.py tests/test_corrections_api.py tests/test_extraction_defense.py tests/test_reports.py tests/test_observability.py tests/test_mcp_connectors.py tests/test_agents.py tests/test_agent_identity.py tests/test_orchestration.py tests/test_premise_gate.py tests/test_safety_fail_closed.py tests/test_chat_hardening.py tests/test_governance.py tests/test_adversarial_defense.py tests/test_tamper_evidence.py tests/test_api.py tests/test_e2e.py tests/test_architecture.py tests/test_middleware.py tests/test_harness.py tests/test_enforcement.py tests/test_vector_store_persistence.py tests/test_embedding_provider_env.py tests/test_learning_hygiene.py tests/test_tenant_integrity.py tests/test_detection_wiring.py tests/test_detection_regressions.py; do
        if [ -f "$f" ]; then
            UNIT_FILES="$UNIT_FILES $f"
        fi
    done

    if [ -n "$UNIT_FILES" ]; then
        if python3 -m pytest $UNIT_FILES --cov=src --cov-fail-under=75 --cov-report=term-missing -x -q --tb=short 2>&1 | tail -20; then
            PYTEST_EXIT=${PIPESTATUS[0]}
            if [ "$PYTEST_EXIT" -eq 0 ]; then
                pass "Unit tests passed"
            else
                fail "Unit tests: some failures (exit code $PYTEST_EXIT)"
            fi
        else
            fail "Unit tests: could not run pytest"
        fi
    else
        warn "No unit test files found in generated project"
    fi

    # Import sweep: every module in the generated package must import
    # cleanly in this step's environment (the pip install above includes
    # fastapi/uvicorn/httpx in every profile, so this does NOT prove
    # imports succeed with only base deps -- it proves e.g. a gateway-off
    # project has no dangling imports of its excluded api/ tree).
    # Caveat: pkgutil.walk_packages silently skips the children of a
    # subpackage whose __init__ fails to import; the parent failure is
    # still reported, child modules are not enumerated.
    IMPORT_SWEEP=$(python3 - "$PROJECT_SLUG" <<'PY'
import importlib
import pkgutil
import sys

package = importlib.import_module(sys.argv[1])
failures = []
for module in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
    name = module.name
    if name.endswith("__main__"):
        continue
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 -- report every import failure
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
for line in failures:
    print(line)
sys.exit(1 if failures else 0)
PY
)
    if [ $? -eq 0 ]; then
        pass "Import sweep: all package modules import cleanly"
    else
        fail "Import sweep: modules failed to import:"
        echo "$IMPORT_SWEEP" | sed 's/^/    /'
    fi
    cd "$REPO_ROOT"
else
    warn "tests/ directory not found in generated project"
fi

# =========================================================================
# Step 9: Injection-Defense Golden Set (deterministic, no LLM)
# =========================================================================
section "Step 9: Injection-Defense Golden Set"
GOLDEN_SET="$GEN_ROOT/evals/tasks/test_injection_defense_golden.py"
if [ -f "$GOLDEN_SET" ]; then
    cd "$GEN_ROOT"
    GOLDEN_OUT=$(python3 evals/tasks/test_injection_defense_golden.py 2>/dev/null)
    GOLDEN_EXIT=$?
    echo "$GOLDEN_OUT" | sed 's/^/    /'
    if [ "$GOLDEN_EXIT" -eq 0 ]; then
        pass "Golden set: no regressions vs baseline"
    else
        fail "Golden set: regression or schema error (exit $GOLDEN_EXIT)"
    fi
    cd "$REPO_ROOT"
elif [ "$INCLUDE_EVALS" = "false" ]; then
    pass "Golden set correctly absent (include_evals=false)"
else
    fail "Golden set missing although include_evals=true"
fi

# =========================================================================
# Step 10: File Structure Check
# =========================================================================
section "Step 10: File Structure"
EXPECTED_DIRS="agents harness llm orchestration security"
if [ "$INCLUDE_API_GATEWAY" = "true" ]; then
    EXPECTED_DIRS="$EXPECTED_DIRS api"
fi
MISSING_DIRS=0
for dir in $EXPECTED_DIRS; do
    if [ -d "$GEN_SRC/$dir" ]; then
        FILE_COUNT=$(find "$GEN_SRC/$dir" -name "*.py" | wc -l | tr -d ' ')
        pass "$dir/ ($FILE_COUNT files)"
    else
        fail "$dir/ missing"
        MISSING_DIRS=$((MISSING_DIRS + 1))
    fi
done

# The learning modules ship in every project regardless of include_learning
# (that toggle only gates optional RAG dependency guidance + docs).
if [ -d "$GEN_SRC/learning" ]; then
    FILE_COUNT=$(find "$GEN_SRC/learning" -name "*.py" | wc -l | tr -d ' ')
    pass "learning/ ($FILE_COUNT files)"
else
    fail "learning/ missing (learning modules ship in every project)"
fi

# =========================================================================
# Step 11: Toggle Wiring (per-profile inclusion/exclusion assertions)
# =========================================================================
# Each fixed toggle must be provably real: the on state ships the files
# (full/defaults profiles), the off state excludes them and scrubs the
# references (gateway-off/minimal profiles).
section "Step 11: Toggle Wiring"

if [ -f "$GEN_ROOT/.copier-answers.yml" ]; then
    pass ".copier-answers.yml present (copier update supported)"
else
    fail ".copier-answers.yml missing -- copier update will not work"
fi

# The Makefile must PARSE in every profile: a jinja tag that eats a
# recipe tab produces "missing separator", which kills every make target
# (make test, make demo, ...), not just the broken one.
if command -v make &>/dev/null; then
    if (cd "$GEN_ROOT" && make -n help >/dev/null 2>&1); then
        pass "Makefile parses (make -n help)"
    else
        fail "Makefile does not parse -- every make target is broken:"
        (cd "$GEN_ROOT" && make -n help 2>&1 | head -3 | sed 's/^/    /')
    fi
else
    warn "make not installed -- skipping Makefile parse check"
fi

# Compact assertion helpers: message, pattern (grep -E), file (relative
# to the generated project root).
has()    { grep -qE "$2" "$GEN_ROOT/$3" && pass "$1" || fail "$1"; }
lacks()  { ! grep -qE "$2" "$GEN_ROOT/$3" 2>/dev/null && pass "$1" || fail "$1"; }
exists() { [ -e "$GEN_ROOT/$2" ] && pass "$1" || fail "$1"; }
absent() { [ ! -e "$GEN_ROOT/$2" ] && pass "$1" || fail "$1"; }

if [ "$INCLUDE_API_GATEWAY" = "true" ]; then
    exists "gateway on: src/$PROJECT_SLUG/api/ present" "src/$PROJECT_SLUG/api"
    has "gateway on: Makefile has serve target" '^serve:' Makefile
    # .env.example must NOT ship an active API_HOST: compose env_file
    # values override image ENV, so an uncommented API_HOST=127.0.0.1
    # would bind uvicorn to the container's loopback and kill the
    # published port while the localhost HEALTHCHECK stays green.
    lacks "gateway on: .env.example API_HOST is commented out" '^API_HOST=' .env.example
    has "gateway on: pyproject has [load] extra" '^load = ' pyproject.toml
    exists "gateway on: tests/load/ harness present" tests/load/locustfile.py
    if [ "$INCLUDE_DEPLOYMENT" = "true" ]; then
        has "gateway on + deploy on: OPERATIONS.md has the compose load recipe" 'make load-test' docs/OPERATIONS.md
        has "gateway on + deploy on: Makefile has load-test target" '^load-test:' Makefile
        has "gateway on: Dockerfile HEALTHCHECK hits /health" 'localhost:8000/health' Dockerfile
        # Belt-and-braces for the same env_file-override bug: compose's
        # environment: block (which outranks env_file) pins the bind.
        has "gateway on: compose pins API_HOST=0.0.0.0 in environment:" 'API_HOST=0.0.0.0' docker-compose.yml
        has "gateway on: compose publishes port 8000" '"8000:8000"' docker-compose.yml
        exists "gateway on: k8s Service present" deploy/k8s/service.yaml
        exists "gateway on: load-test compose override present" docker-compose.load.yml
    else
        # gateway on + deployment off: the load-test recipe must not point
        # at compose artifacts that were excluded -- OPERATIONS.md carries
        # the serve-based variant instead.
        lacks "gateway on + deploy off: Makefile has no load-test references" 'load-test' Makefile
        lacks "gateway on + deploy off: OPERATIONS.md has no compose-load reference" 'docker-compose\.load\.yml' docs/OPERATIONS.md
        has "gateway on + deploy off: OPERATIONS.md has the serve-based load recipe" 'make serve-prod' docs/OPERATIONS.md
        lacks "gateway on + deploy off: .env.example has no compose-load reference" 'docker-compose\.load\.yml' .env.example
    fi
else
    absent "gateway off: src/$PROJECT_SLUG/api/ excluded" "src/$PROJECT_SLUG/api"
    lacks "gateway off: Makefile has no serve target" '^serve:' Makefile
    has "gateway off: httpx still a base dependency (remote agents)" '^httpx' requirements.txt
    # The load harness presupposes an HTTP server.
    lacks "gateway off: pyproject has no [load] extra" '^load = ' pyproject.toml
    absent "gateway off: tests/load/ excluded" tests/load
    # Docs must not advertise HTTP-server workflows that do not exist.
    lacks "gateway off: OPERATIONS.md has no serve-prod reference" 'serve-prod' docs/OPERATIONS.md
    lacks "gateway off: OPERATIONS.md cites no api/ modules" 'api/gateway\.py|api/routes/|api/middleware/' docs/OPERATIONS.md
    lacks "gateway off: README advertises no HTTP endpoint" 'localhost:8000' README.md
    if [ "$INCLUDE_DEPLOYMENT" = "true" ]; then
        # gateway-off + deployment-on: the image CMD prints usage and
        # exits, so nothing may publish port 8000 or restart-loop it.
        lacks "gateway off: Dockerfile has no HTTP HEALTHCHECK" 'localhost:8000/health' Dockerfile
        lacks "gateway off: compose publishes no port" '"8000:8000"' docker-compose.yml
        has "gateway off: compose documents the command: override" 'command:' docker-compose.yml
        absent "gateway off: k8s Service excluded" deploy/k8s/service.yaml
        lacks "gateway off: k8s deployment exposes no containerPort" 'containerPort' deploy/k8s/deployment.yaml
        absent "gateway off: load-test compose override excluded" docker-compose.load.yml
        lacks "gateway off: Makefile k8s-status queries no Service" 'kubectl get svc' Makefile
    fi
fi

if [ "$INCLUDE_DEPLOYMENT" = "true" ]; then
    DEPLOY_OK=1
    for f in Dockerfile docker-compose.yml .dockerignore; do
        [ -e "$GEN_ROOT/$f" ] || { DEPLOY_OK=0; fail "deployment on: $f missing"; }
    done
    [ -d "$GEN_ROOT/deploy" ] || { DEPLOY_OK=0; fail "deployment on: deploy/ missing"; }
    [ "$DEPLOY_OK" = "1" ] && pass "deployment on: Dockerfile, docker-compose.yml, .dockerignore, deploy/ present"
    has "deployment on: Makefile has docker targets" '^docker-build:' Makefile
else
    DEPLOY_GONE=1
    for f in Dockerfile docker-compose.yml docker-compose.load.yml .dockerignore; do
        [ ! -e "$GEN_ROOT/$f" ] || { DEPLOY_GONE=0; fail "deployment off: $f still generated"; }
    done
    [ ! -d "$GEN_ROOT/deploy" ] || { DEPLOY_GONE=0; fail "deployment off: deploy/ still generated"; }
    [ "$DEPLOY_GONE" = "1" ] && pass "deployment off: Dockerfile, docker-compose*, .dockerignore, deploy/ excluded"
    lacks "deployment off: Makefile has no docker targets" '^docker-build:' Makefile
    lacks "deployment off: Makefile has no k8s targets" '^k8s-deploy:' Makefile
fi

if [ "$INCLUDE_EVALS" = "true" ]; then
    exists "evals on: evals/ present" evals
    has "evals on: setup_check checks evals/" '"evals"' scripts/setup_check.py
else
    absent "evals off: evals/ excluded" evals
    lacks "evals off: setup_check has no evals reference" 'evals' scripts/setup_check.py
fi

# =========================================================================
# Summary
# =========================================================================
echo ""
echo "==========================================="
echo "  RESULTS: $PASS_COUNT passed, $FAIL_COUNT failed, $WARN_COUNT warnings"
echo "  Config:  $PROJECT_TYPE / $LLM_PROVIDER / $PERSISTENCE / profile=$PROFILE"
echo "==========================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo ""
    echo "✗ VALIDATION FAILED ($FAIL_COUNT failures)"
    exit 1
else
    echo ""
    echo "✓ VALIDATION PASSED"
    exit 0
fi
