#!/usr/bin/env bash
# validate_generated.sh -- Generate a test project and run the full validation suite.
#
# Usage: bash scripts/validate_generated.sh [project_type] [llm_provider] [persistence]
#   Default: web-app anthropic sqlite
#
# Steps:
#   1. Generate a test project via copier (non-interactive)
#   2. Run ruff (linting)
#   3. Run bandit (security)
#   4. Run architecture tests
#   5. Run red team checks
#   6. Run AI-specific checks
#   7. Run automated agent review
#   8. Clean up
#
# Exit code 0 = all passed, non-zero = failures found.

set -uo pipefail

PROJECT_TYPE="${1:-web-app}"
LLM_PROVIDER="${2:-anthropic}"
PERSISTENCE="${3:-sqlite}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
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
section "Step 0: Quick Checks (templates)"
if python3 "$SCRIPT_DIR/quick_checks.py"; then
    pass "Quick checks"
else
    fail "Quick checks -- see output above"
fi

# =========================================================================
# Step 1: Generate test project
# =========================================================================
section "Step 1: Generate Test Project ($PROJECT_TYPE / $LLM_PROVIDER / $PERSISTENCE)"
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
        --data include_evals=true \
        --data include_state_management=true \
        --data include_llm_client=true \
        --data include_api_gateway=true \
        --data include_deployment=true \
        --data include_learning=true \
        >"$COPIER_LOG" 2>&1)
    COPIER_STATUS=$?

    if [ "$COPIER_STATUS" -ne 0 ] && [ "${ALLOW_COPIER_FALLBACK:-}" = "1" ]; then
        warn "copier failed; using direct Jinja render fallback for sandbox validation"
        python3 - "$REPO_ROOT/template" "$TEST_DIR/$PROJECT_SLUG" \
            "$PROJECT_NAME" "$PROJECT_SLUG" "$PROJECT_TYPE" "$LAYERS" "$LLM_PROVIDER" "$PERSISTENCE" <<'PY'
import sys
from pathlib import Path

from jinja2 import Environment, StrictUndefined

template_root = Path(sys.argv[1])
destination_root = Path(sys.argv[2])

context = {
    "project_name": sys.argv[3],
    "project_slug": sys.argv[4],
    "project_description": "Validation test project",
    "author_name": "CI",
    "project_type": sys.argv[5],
    "layers": sys.argv[6],
    "llm_provider": sys.argv[7],
    "persistence": sys.argv[8],
    "python_version": "3.13",
    "include_evals": True,
    "include_state_management": True,
    "include_llm_client": True,
    "include_api_gateway": True,
    "include_deployment": True,
    "include_learning": True,
}

env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)

for source in template_root.rglob("*"):
    relative = source.relative_to(template_root)
    if ".cursor" in relative.parts:
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

generated_project_root = destination_root / context["project_slug"]

for layer in context["layers"].split(","):
    layer_dir = generated_project_root / layer.strip()
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / "__init__.py").touch()

for eval_dir in ["evals/capability", "evals/regression", "evals/graders", "evals/results"]:
    (generated_project_root / eval_dir).mkdir(parents=True, exist_ok=True)
(generated_project_root / "evals" / "__init__.py").touch()
(generated_project_root / "evals" / "graders" / "__init__.py").touch()
PY
    fi
    
    # Copier creates: TEST_DIR/PROJECT_SLUG/PROJECT_SLUG/ (nested)
    GENERATED_DIR="$TEST_DIR/$PROJECT_SLUG/$PROJECT_SLUG"
    if [ ! -d "$GENERATED_DIR/src/$PROJECT_SLUG" ]; then
        # Try without nesting (some copier versions)
        GENERATED_DIR="$TEST_DIR/$PROJECT_SLUG"
    fi

    if [ -d "$GENERATED_DIR/src/$PROJECT_SLUG" ]; then
        pass "Project generated at $GENERATED_DIR"
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
# Step 8: Unit Tests (run pytest on generated project)
# =========================================================================
section "Step 8: Unit Tests"
if [ -d "$GEN_ROOT/tests" ]; then
    cd "$GEN_ROOT"
    # Install project dependencies quietly (skip LLM providers to avoid API key issues)
    pip install -q pytest pytest-asyncio pytest-cov fastapi uvicorn httpx pydantic python-dotenv 2>/dev/null
    if [ -f "pyproject.toml" ]; then
        pip install -q -e . 2>/dev/null || warn "Editable install failed -- tests may rely on repo-root imports"
    fi

    # Run all test files (all use mocks/in-process testing, no external deps)
    UNIT_FILES=""
    for f in tests/test_security.py tests/test_injection_defense.py tests/test_llm.py tests/test_single_shot.py tests/test_learning.py tests/test_learning_maturity.py tests/test_learning_store.py tests/test_corrections_api.py tests/test_mcp_connectors.py tests/test_agents.py tests/test_agent_identity.py tests/test_orchestration.py tests/test_chat_hardening.py tests/test_governance.py tests/test_adversarial_defense.py tests/test_tamper_evidence.py tests/test_api.py tests/test_e2e.py tests/test_architecture.py tests/test_middleware.py tests/test_harness.py tests/test_enforcement.py; do
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
    cd "$REPO_ROOT"
else
    warn "tests/ directory not found in generated project"
fi

# =========================================================================
# Step 9: File Structure Check
# =========================================================================
section "Step 8: File Structure"
EXPECTED_DIRS="agents api harness llm orchestration security"
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

# Check learning dir only if include_learning was set
if [ -d "$GEN_SRC/learning" ]; then
    FILE_COUNT=$(find "$GEN_SRC/learning" -name "*.py" | wc -l | tr -d ' ')
    pass "learning/ ($FILE_COUNT files)"
fi

# =========================================================================
# Summary
# =========================================================================
echo ""
echo "==========================================="
echo "  RESULTS: $PASS_COUNT passed, $FAIL_COUNT failed, $WARN_COUNT warnings"
echo "  Config:  $PROJECT_TYPE / $LLM_PROVIDER / $PERSISTENCE"
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
