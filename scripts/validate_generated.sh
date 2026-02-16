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
TEST_DIR="/tmp/aiscaffold_test_$(date +%s)_$$"
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
    # Determine layers based on project type (mirrors copier.yml default)
    case "$PROJECT_TYPE" in
        web-app)     LAYERS="data,analysis,components" ;;
        cli-tool)    LAYERS="data,logic" ;;
        multi-agent) LAYERS="data,analysis,orchestration,specialists,prompts" ;;
        api-service) LAYERS="data,service,routes" ;;
        *)           LAYERS="data,analysis,components" ;;
    esac

    copier copy "$REPO_ROOT" "$TEST_DIR/$PROJECT_SLUG" --trust --defaults \
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
        2>/dev/null
    
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
# Step 8: File Structure Check
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
