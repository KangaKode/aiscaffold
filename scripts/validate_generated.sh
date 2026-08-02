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
import os
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2])

# Depth-aware ignore: '.cursor' is ignored ONLY at the repository root
# (that is the maintainer-only rule set for THIS repo, which must not
# leak into generated projects). The template's own '.cursor/' tree
# under 'template/{{project_slug}}/.cursor/' must survive the copy so
# copier can render it into generated output. Every other entry is a
# transient/cache directory we drop at every depth.
NAMES_IGNORED_ANY_DEPTH = {
    ".git",
    ".validation-venv",
    ".tmp-copier-debug",
    ".tmp-validation",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    # Not part of the template surface; these are session/plan artefacts
    # that must never enter a rendered project.
    ".aiscaffold",
    ".superpowers",
    "data",
}

# Manual walk (not shutil.copytree) so we can copy file contents
# without shutil.copystat on directories. macOS marks '.cursor/**'
# with the 'com.apple.provenance' xattr, which shutil.copystat refuses
# to propagate for unprivileged processes -- the resulting EPERM
# regressed the entire generation step before this rewrite.
destination.mkdir(parents=True, exist_ok=True)
for current, dirs, files in os.walk(source):
    current_path = Path(current)
    dirs[:] = [d for d in dirs if d not in NAMES_IGNORED_ANY_DEPTH]
    if current_path == source:
        dirs[:] = [d for d in dirs if d != ".cursor"]
    relative = current_path.relative_to(source)
    target_dir = destination / relative
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        src_file = current_path / name
        dst_file = target_dir / name
        try:
            shutil.copyfile(src_file, dst_file)
        except (OSError, shutil.SameFileError):
            # Preserve legacy behaviour: unreadable files should not
            # abort generation validation.
            continue
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
    # The template's nested .cursor tree ships into generated projects.
    # The Copier _subdirectory setting (template/{{project_slug}}) already
    # prevents the maintainer-only root .cursor from ever entering the
    # sandbox render, so no blanket .cursor skip is needed here.
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
# Step 7b: Reviewer-eval deterministic runner
# =========================================================================
# The shipped reviewer-eval runner (scripts/reviewer_eval.py) validates
# the seeded reviewer-evals/cases.json fixture and exercises every
# DETERMINISTIC case through red_team_check. Exits nonzero on schema
# errors, missed vulnerabilities, or false-blocks on safe near-misses.
# MANUAL_AGENT cases are NOT executed here (CI does not run prompt
# reviewers); see reviewer-evals/README.md for the manual-review recipe.
section "Step 7b: Reviewer-eval deterministic runner"
REVIEWER_EVAL_RUNNER="$GEN_ROOT/scripts/reviewer_eval.py"
REVIEWER_EVAL_CASES="$GEN_ROOT/reviewer-evals/cases.json"
if [ -f "$REVIEWER_EVAL_RUNNER" ] && [ -f "$REVIEWER_EVAL_CASES" ]; then
    if (cd "$GEN_ROOT" && python3 scripts/reviewer_eval.py --quiet >/dev/null 2>&1); then
        pass "Reviewer-eval runner: DETERMINISTIC cases pass"
    else
        fail "Reviewer-eval runner: failures reported (rerun locally without --quiet for detail)"
        (cd "$GEN_ROOT" && python3 scripts/reviewer_eval.py 2>&1 | sed 's/^/    /' | head -40)
    fi
else
    fail "Reviewer-eval runner or cases.json missing in generated project"
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
    for f in tests/test_security.py tests/test_injection_defense.py tests/test_ingest_scan.py tests/test_llm.py tests/test_single_shot.py tests/test_learning.py tests/test_learning_maturity.py tests/test_learning_store.py tests/test_store_correctness.py tests/test_async_hardening.py tests/test_learning_wiring.py tests/test_corrections_api.py tests/test_supersession.py tests/test_extraction_defense.py tests/test_procedures.py tests/test_reports.py tests/test_observability.py tests/test_mcp_connectors.py tests/test_mcp_tool_screen.py tests/test_agents.py tests/test_agent_identity.py tests/test_orchestration.py tests/test_premise_gate.py tests/test_safety_fail_closed.py tests/test_chat_hardening.py tests/test_governance.py tests/test_adversarial_defense.py tests/test_tamper_evidence.py tests/test_api.py tests/test_e2e.py tests/test_architecture.py tests/test_middleware.py tests/test_harness.py tests/test_enforcement.py tests/test_vector_store_persistence.py tests/test_embedding_provider_env.py tests/test_learning_hygiene.py tests/test_retrieval_ranking.py tests/test_trust_guard.py tests/test_context_pressure.py tests/test_loop_integrity.py tests/test_adversarial_open_corpus.py tests/test_public_corpus_isolation.py tests/test_tenant_integrity.py tests/test_detection_wiring.py tests/test_credential_currency.py tests/test_detection_regressions.py; do
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
# Step 9b: Public-corpus harness (deterministic, no LLM; NOT in UNIT_FILES)
# =========================================================================
section "Step 9b: Public-corpus harness"
PUBLIC_CORPUS_HARNESS="$GEN_ROOT/evals/tasks/test_public_corpus_harness.py"
if [ -f "$PUBLIC_CORPUS_HARNESS" ]; then
    cd "$GEN_ROOT"
    # Capture stdout+stderr so import/schema failures are visible on CI fail.
    PUBLIC_OUT=$(python3 evals/tasks/test_public_corpus_harness.py 2>&1)
    PUBLIC_EXIT=$?
    echo "$PUBLIC_OUT" | sed 's/^/    /'
    if [ "$PUBLIC_EXIT" -eq 0 ]; then
        pass "Public-corpus harness: no regressions vs baseline"
    else
        fail "Public-corpus harness: regression or schema error (exit $PUBLIC_EXIT)"
    fi
    cd "$REPO_ROOT"
elif [ "$INCLUDE_EVALS" = "false" ]; then
    pass "Public-corpus harness correctly absent (include_evals=false)"
else
    fail "Public-corpus harness missing although include_evals=true"
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
# Multiline-aware lacks: Python re.DOTALL so patterns can span line breaks
# (grep -E is line-oriented and misses cross-line Bugbot-plus-domain wording).
lacks_re() {
    local msg="$1" pattern="$2" rel="$3"
    if python3 -c "import pathlib,re,sys; t=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'); sys.exit(1 if re.search(sys.argv[2], t, re.I|re.S) else 0)" \
        "$GEN_ROOT/$rel" "$pattern"
    then
        pass "$msg"
    else
        fail "$msg"
    fi
}
exists() { [ -e "$GEN_ROOT/$2" ] && pass "$1" || fail "$1"; }
absent() { [ ! -e "$GEN_ROOT/$2" ] && pass "$1" || fail "$1"; }

# Risk-tier policy + design ceremony ship in EVERY profile (docs/,
# .cursor/rules/, .cursor/agents/ are ungated by include_*). The Task 1
# tier-policy tests already prove documentation parity at the template
# source; these assertions prove copier ACTUALLY RENDERED that source
# into the generated project (a source-text check alone would not catch
# a regression like the old '.cursor'-blanket-skip fallback renderer).
exists "risk-tier: development-process rule rendered into generated project" .cursor/rules/development-process.mdc
has "risk-tier: rule frontmatter has alwaysApply: true" '^alwaysApply: true$' .cursor/rules/development-process.mdc
has "risk-tier: rule mentions risk-tier framing" 'risk-tier' .cursor/rules/development-process.mdc
has "risk-tier: rule pins 'highest applicable tier wins'" 'highest applicable tier wins' .cursor/rules/development-process.mdc
has "risk-tier: rule includes 'additions plus deletions'" 'additions plus deletions' .cursor/rules/development-process.mdc
has "risk-tier: rule names threat model as the 4th High artifact" 'threat model' .cursor/rules/development-process.mdc
has "risk-tier: process doc pins 'highest applicable tier wins'" 'highest applicable tier wins' docs/DEVELOPMENT_PROCESS.md
has "risk-tier: process doc includes 'additions plus deletions'" 'additions plus deletions' docs/DEVELOPMENT_PROCESS.md
has "risk-tier: process doc requires threat model" '[Tt]hreat [Mm]odel' docs/DEVELOPMENT_PROCESS.md
has "risk-tier: process doc references docs/designs/<feature>/ layout" 'docs/designs/<feature>/' docs/DEVELOPMENT_PROCESS.md
exists "design-doc-author agent rendered into generated project" .cursor/agents/design-doc-author.md
has "design-doc-author: names threat model as required" 'THREAT_MODEL.md' .cursor/agents/design-doc-author.md
has "design-doc-author: uses docs/designs/<feature>/ primary layout" 'docs/designs/<feature>/' .cursor/agents/design-doc-author.md
has "design-doc-author: states four design artifacts for High" 'four' .cursor/agents/design-doc-author.md
has "INDEX: DEVELOPMENT_PROCESS entry mentions risk-tier policy" 'DEVELOPMENT_PROCESS.md.*[Rr]isk-tier' docs/INDEX.md
has "INDEX: threat models section documented" '[Tt]hreat [Mm]odels' docs/INDEX.md
lacks "INDEX: no legacy three-document assumption in Phased Model refs" 'three design documents' docs/INDEX.md

# Task 7 -- bug-class register + closed-loop completion gate must render in
# every profile. The root repo's BUG_CLASS_REGISTER.md is maintainer-only
# and is NOT rendered into generated projects; only the template register
# under template/{{project_slug}}/docs/ is copier-rendered. Assertions
# below target the RENDERED generated-project copy.
exists "bug-class: register rendered into generated project" docs/BUG_CLASS_REGISTER.md
has "bug-class: register names 'recurring bug class'" '[Rr]ecurring bug class' docs/BUG_CLASS_REGISTER.md
has "bug-class: register carries closed-vocabulary hint (DRAFT)" '`DRAFT`' docs/BUG_CLASS_REGISTER.md
has "bug-class: register carries closed-vocabulary hint (SHADOW)" '`SHADOW`' docs/BUG_CLASS_REGISTER.md
has "bug-class: register carries closed-vocabulary hint (BLOCKING)" '`BLOCKING`' docs/BUG_CLASS_REGISTER.md
has "bug-class: register carries closed-vocabulary hint (SUSPENDED)" '`SUSPENDED`' docs/BUG_CLASS_REGISTER.md
has "bug-class: register begins empty (no invented history)" 'none yet' docs/BUG_CLASS_REGISTER.md
has "bug-class: register schema names regression test field" '[Rr]egression test' docs/BUG_CLASS_REGISTER.md
has "bug-class: register schema names owner field" 'Owner' docs/BUG_CLASS_REGISTER.md
has "bug-class: register denies self-edit of own rules" 'self-edit' docs/BUG_CLASS_REGISTER.md
has "bug-class: process doc classifies as one-off vs recurring" 'one-off.*recurring|recurring.*one-off' docs/DEVELOPMENT_PROCESS.md
has "bug-class: process doc names regression test in completion gate" 'regression test' docs/DEVELOPMENT_PROCESS.md
has "bug-class: process doc names rule/instruction update artifact" 'agent rule or instruction' docs/DEVELOPMENT_PROCESS.md
has "bug-class: process doc references the register path" 'BUG_CLASS_REGISTER.md' docs/DEVELOPMENT_PROCESS.md
has "bug-class: process doc states agents cannot auto-classify" 'cannot auto-classify' docs/DEVELOPMENT_PROCESS.md
lacks "bug-class: process doc does not overclaim CI enforcement of the gate" 'CI enforces the (three-artifact|completion) (gate|bar)' docs/DEVELOPMENT_PROCESS.md
has "bug-class: dev-process rule references the register path" 'BUG_CLASS_REGISTER.md' .cursor/rules/development-process.mdc
has "bug-class: dev-process rule mentions recurring bug class" 'recurring bug class' .cursor/rules/development-process.mdc
# Bugbot honesty: generated projects fulfill review via shipped agents;
# .mdc rules do not configure Bugbot; Autofix stays off by default.
has "bugbot-honesty: process names code-reviewer for post-diff review" 'code-reviewer' docs/DEVELOPMENT_PROCESS.md
has "bugbot-honesty: process names sast-reviewer for post-diff review" 'sast-reviewer' docs/DEVELOPMENT_PROCESS.md
has "bugbot-honesty: process pins .mdc does not configure Bugbot" 'do \*\*not\*\* configure Bugbot' docs/DEVELOPMENT_PROCESS.md
has "bugbot-honesty: process pins Autofix stays off" 'Autofix stays off' docs/DEVELOPMENT_PROCESS.md
has "bugbot-honesty: rule names code-reviewer for post-diff review" 'code-reviewer' .cursor/rules/development-process.mdc
lacks_re "bugbot-honesty: rule does not require Bugbot-plus-domain path" 'Bugbot.{0,120}plus.{0,80}matching domain (expert|reviewer)' .cursor/rules/development-process.mdc
lacks_re "bugbot-honesty: process does not require Bugbot-plus-domain path" 'Bugbot.{0,120}plus.{0,80}matching domain (expert|reviewer)' docs/DEVELOPMENT_PROCESS.md
has "bugbot-honesty: GOVERNANCE Non-Claim Auto-review not security boundary" 'not a security boundary' docs/GOVERNANCE.md
has "bugbot-honesty: GOVERNANCE Non-Claim no hard dep on Bugbot" 'No hard dependency on Approval Agents or Bugbot' docs/GOVERNANCE.md
# done-claim-verifier: readonly implementer done-check (not Sentinel, not
# a REVIEWER_ASSURANCE BLOCKING gate). Ships in every profile.
exists "done-claim-verifier: agent rendered into generated project" .cursor/agents/done-claim-verifier.md
has "done-claim-verifier: frontmatter name" '^name: done-claim-verifier$' .cursor/agents/done-claim-verifier.md
has "done-claim-verifier: readonly true" '^readonly: true$' .cursor/agents/done-claim-verifier.md
has "done-claim-verifier: denies merge authority" 'Do not merge' .cursor/agents/done-claim-verifier.md
has "done-claim-verifier: not product Sentinel" 'not.*product Sentinel|not product Sentinel' .cursor/agents/done-claim-verifier.md
has "done-claim-verifier: process doc names the agent" 'done-claim-verifier' docs/DEVELOPMENT_PROCESS.md
has "done-claim-verifier: process rule names the agent" 'done-claim-verifier' .cursor/rules/development-process.mdc
has "bug-class: INDEX links the bug-class register" 'BUG_CLASS_REGISTER.md' docs/INDEX.md
has "bug-class: expert-review names authority boundary" '[Aa]uthority [Bb]oundary' .cursor/rules/expert-review.mdc
has "bug-class: expert-review denies self-promotion" 'self-promot' .cursor/rules/expert-review.mdc
# Red-team frontmatter honesty: description must not claim it "blocks
# commits" -- analysis always runs, blocking is gated on assurance status.
lacks "bug-class: red-team frontmatter no longer claims 'blocks commits'" 'blocks commits with security issues' .cursor/rules/red-team.mdc
has "bug-class: red-team frontmatter references assurance register" 'REVIEWER_ASSURANCE.md' .cursor/rules/red-team.mdc

# Task 4 -- rendered CI security job must ship in EVERY profile. These are
# source-text assertions on the RENDERED workflow (the .jinja is copier's
# input; the .yml here is copier's output), so they prove copier actually
# emitted the security job into a generated project rather than trusting
# a template-side unit test. The unit-level contract (structural YAML,
# raw-block wrapping, extras conditional on include_api_gateway) lives in
# tests/test_ci_security.py; these are the belt-and-braces install check.
exists "security: rendered CI workflow ships" .github/workflows/ci.yml
has "security: rendered CI defines a top-level security job" '^  security:' .github/workflows/ci.yml
has "security: rendered checkout uses fetch-depth: 0" 'fetch-depth: 0' .github/workflows/ci.yml
has "security: Gitleaks action pinned to the commit SHA" \
    'gitleaks/gitleaks-action@ff98106e4c7b2bc287b24eaf42907196329070c7' .github/workflows/ci.yml
has "security: pip-audit pinned to 2.10.1 in rendered workflow" \
    'pip-audit==2.10.1' .github/workflows/ci.yml
has "security: GITHUB_TOKEN github expression survives copier rendering" \
    'secrets\.GITHUB_TOKEN' .github/workflows/ci.yml
has "security: audit-gate invocation renders into workflow" \
    'scripts/pip_audit_gate\.py' .github/workflows/ci.yml
exists "security: audit-gate script ships in generated project" scripts/pip_audit_gate.py
exists "security: exceptions file rendered into generated project" \
    .github/pip-audit-exceptions.json
has "security: exceptions file starts as an empty JSON list" \
    '^\[\]' .github/pip-audit-exceptions.json
has "security: summary aggregates test, lint, security" \
    'needs:.*test.*lint.*security' .github/workflows/ci.yml
lacks "security: no scanner --fix flag in rendered workflow" \
    '[^-]--fix\b' .github/workflows/ci.yml
lacks "security: no continue-on-error bypass in rendered workflow" \
    'continue-on-error:[[:space:]]*true' .github/workflows/ci.yml
lacks "security: no |\| true bypass in rendered workflow" \
    '\|\|[[:space:]]*true' .github/workflows/ci.yml
# The scaffold contract forbids ANY Gitleaks allowlist/ignore config
# from shipping into a generated project (fixtures are cleaned at the
# source; first-run Gitleaks in a generated repo must be clean without
# any shipped suppression).
absent "security: no .gitleaks.toml ships into generated project" .gitleaks.toml
absent "security: no gitleaks.toml ships into generated project" gitleaks.toml
absent "security: no .gitleaksignore ships into generated project" .gitleaksignore
# Generated OPERATIONS.md names the summary job as the required
# branch-protection check (the co-occurrence contract is unit-tested;
# this is the render check).
has "security: OPERATIONS.md documents branch protection" \
    'branch protection' docs/OPERATIONS.md
has "security: OPERATIONS.md names summary as the required check" \
    'summary' docs/OPERATIONS.md
has "security: OPERATIONS.md marks the check as required" \
    'required' docs/OPERATIONS.md
# Governance Non-Claims for the new scanners must render.
has "security: GOVERNANCE names pip-audit non-lockfile limit" \
    'pip-audit.*resolved environment' docs/GOVERNANCE.md
has "security: GOVERNANCE names advisory-database limits" \
    'advisory database has limits' docs/GOVERNANCE.md
has "security: GOVERNANCE flags branch-protection external-only" \
    'Branch protection cannot be configured' docs/GOVERNANCE.md
has "security: GOVERNANCE flags exceptions file as human review" \
    'exceptions file is a HUMAN review record' docs/GOVERNANCE.md

# Task 8 -- reviewer-eval fixture, README, shipped runner, and CI wiring must
# render in EVERY profile. The runner is not gated by include_evals: it
# exercises red_team_check against the seeded corpus and ships alongside
# other security scripts (pip_audit_gate, red_team_check). Per-profile
# render assertions belt-and-brace the unit-level contract already covered
# by tests/test_reviewer_evals.py.
exists "reviewer-evals: fixture ships in all profiles" reviewer-evals/cases.json
exists "reviewer-evals: README ships in all profiles" reviewer-evals/README.md
exists "reviewer-evals: shipped runner ships in all profiles" scripts/reviewer_eval.py
has "reviewer-evals: README documents DETERMINISTIC mode" \
    'DETERMINISTIC' reviewer-evals/README.md
has "reviewer-evals: README documents MANUAL_AGENT mode" \
    'MANUAL_AGENT' reviewer-evals/README.md
has "reviewer-evals: README publishes coverage matrix" \
    'Coverage matrix|coverage matrix' reviewer-evals/README.md
has "reviewer-evals: README lists hardcoded_secret domain" \
    'hardcoded_secret' reviewer-evals/README.md
has "reviewer-evals: README lists sql_injection domain" \
    'sql_injection' reviewer-evals/README.md
has "reviewer-evals: README lists unsafe_shell domain" \
    'unsafe_shell' reviewer-evals/README.md
has "reviewer-evals: README lists path_traversal domain" \
    'path_traversal' reviewer-evals/README.md
has "reviewer-evals: README lists missing_auth domain" \
    'missing_auth' reviewer-evals/README.md
has "reviewer-evals: README lists missing_tenant_scope domain" \
    'missing_tenant_scope' reviewer-evals/README.md
has "reviewer-evals: README lists prompt_injection_boundary domain" \
    'prompt_injection_boundary' reviewer-evals/README.md
has "reviewer-evals: README lists reviewer_injection_resistance domain" \
    'reviewer_injection_resistance' reviewer-evals/README.md
lacks "reviewer-evals: README does not overclaim manual as deterministic" \
    'path traversal is deterministically|missing auth is deterministically|prompt injection is deterministically|ci runs prompt reviewers' \
    reviewer-evals/README.md
has "reviewer-evals: runner has --quiet flag documented" \
    '"--quiet"' scripts/reviewer_eval.py
# The security CI job invokes the runner unconditionally (reviewer-evals
# ship in every profile). Assert the invocation renders in the workflow.
has "reviewer-evals: CI security job runs reviewer_eval.py" \
    'scripts/reviewer_eval\.py' .github/workflows/ci.yml
# The runner ships as a plain .py; a stray .jinja suffix would send the
# raw template into a generated project.
absent "reviewer-evals: runner has no leftover .jinja copy" \
    scripts/reviewer_eval.py.jinja
# The seeded corpus is a JSON list with the 16 cases the fixture ships
# (8 domains x vulnerable/safe pair, including reviewer_injection_resistance).
if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d,list) and len(d)==16 else 1)" \
        "$GEN_ROOT/reviewer-evals/cases.json" 2>/dev/null; then
    pass "reviewer-evals: cases.json is a 16-entry JSON list"
else
    fail "reviewer-evals: cases.json is not a 16-entry JSON list (8 domains x vulnerable/safe pair)"
fi
# Belt-and-braces: no committed line contains the assembled fake credential
# marker. tests/test_reviewer_evals.py asserts this at the unit level; the
# shell check catches regressions where the fixture is edited without
# re-running the tests.
if grep -RFq 'password = "seed-abc123def4560"' "$GEN_ROOT/reviewer-evals" 2>/dev/null; then
    fail "reviewer-evals: assembled fake-credential marker leaked into fixture text"
else
    pass "reviewer-evals: no assembled fake-credential marker in fixture text"
fi

# Task 9 -- reviewer-assurance shadow/promotion contract must render in
# EVERY profile. The template counterpart lives at
# template/{{project_slug}}/docs/REVIEWER_ASSURANCE.md; these assertions
# target the RENDERED generated-project copy so a regression in copier
# rendering (e.g. a doc dropped by an errant _exclude glob) is caught
# alongside the source-text unit tests in tests/test_review_governance.py.
exists "reviewer-assurance: doc rendered into generated project" docs/REVIEWER_ASSURANCE.md
exists "reviewer-assurance: baseline v2 artifact ships" docs/reviewer-evals/baseline-v2.md
exists "reviewer-assurance: promotion proposal v1 ships" docs/reviewer-evals/promotion-proposal-v1.md
has "reviewer-assurance: register points at baseline v2" \
    'baseline-v2\.md' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: register points at promotion proposal v1" \
    'promotion-proposal-v1\.md' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: baseline history names NOT_EVALUATED" \
    'NOT_EVALUATED' docs/REVIEWER_ASSURANCE.md
# State vocabulary (closed to DRAFT / SHADOW / BLOCKING / SUSPENDED).
has "reviewer-assurance: state DRAFT documented" '`DRAFT`' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: state SHADOW documented" '`SHADOW`' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: state BLOCKING documented" '`BLOCKING`' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: state SUSPENDED documented" '`SUSPENDED`' docs/REVIEWER_ASSURANCE.md
# Promotion contract: five gates and the human-approval requirement.
has "reviewer-assurance: promotion criteria heading" \
    '[Pp]romotion [Cc]riteria' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion gate 1 all vulnerable detected" \
    'll [Vv]ulnerable [Cc]ases [Dd]etected' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion gate 2 zero false blocking on safe" \
    'ero [Ff]alse [Bb]locking' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion gate 3 complete evidence" \
    'omplete [Ee]vidence' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion gate 4 injection resistance" \
    'njection [Rr]esistance' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion gate 5 recorded human approval" \
    'ecorded [Hh]uman [Aa]pproval' docs/REVIEWER_ASSURANCE.md
# Material-change reset (prompt / scope / tools / model behavior) plus
# the behavior-neutral editorial exemption.
has "reviewer-assurance: material change returns row to SHADOW" \
    '[Mm]aterial [Cc]hange' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: material change names prompt" \
    'prompt' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: material change names scope" \
    'scope' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: material change names tools" \
    'tools' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: material change names model behavior" \
    'model behavior' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: behavior-neutral editorial exemption" \
    '[Bb]ehavior-neutral editorial' docs/REVIEWER_ASSURANCE.md
# Suspension triggers: four events that demote to SUSPENDED.
has "reviewer-assurance: suspension trigger missed seeded cases" \
    '[Mm]issed [Ss]eeded [Cc]ases' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: suspension trigger false blocking" \
    '[Ff]alse [Bb]locking' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: suspension trigger instruction-following untrusted" \
    '[Ii]nstruction-[Ff]ollowing [Ff]rom [Uu]ntrusted' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: suspension trigger scope overreach" \
    '[Ss]cope [Oo]verreach' docs/REVIEWER_ASSURANCE.md
# Manual vs deterministic honesty: prompt-reviewer runs are manual because
# no authenticated agent runner exists in CI.
has "reviewer-assurance: prompt-reviewer runs are manual" \
    '[Pp]rompt-[Rr]eviewer [Rr]uns[^\.]*[Mm]anual' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: no authenticated agent runner in CI" \
    'no authenticated agent runner' docs/REVIEWER_ASSURANCE.md
# Downstream promotion procedure: run shipped command, feed MANUAL_AGENT
# cases in fresh contexts, record case IDs, obtain human approval.
has "reviewer-assurance: names shipped runner scripts/reviewer_eval.py" \
    'scripts/reviewer_eval\.py' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: names MANUAL_AGENT case scope" \
    'MANUAL_AGENT' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: fresh contexts requirement" \
    '[Ff]resh [Cc]ontext' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: record case IDs and evidence" \
    'case ID' docs/REVIEWER_ASSURANCE.md
# Promotion record schema fields.
has "reviewer-assurance: promotion record schema heading" \
    '[Pp]romotion [Rr]ecord [Ss]chema' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Reviewer" \
    'Reviewer' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Version" \
    'Version' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Fixture-set version" \
    '[Ff]ixture-set version' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Detection result" \
    '[Dd]etection result' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Safe-case result" \
    '[Ss]afe-case result' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Injection-resistance result" \
    '[Ii]njection-resistance result' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Evidence review" \
    '[Ee]vidence review' docs/REVIEWER_ASSURANCE.md
has "reviewer-assurance: promotion record field Human approver" \
    '[Hh]uman approver' docs/REVIEWER_ASSURANCE.md
# No reviewer row currently BLOCKING (Task 10 records baselines).
# Use POSIX character classes because BSD grep does not honour \s/[^\n].
if grep -qE '^\|.+`BLOCKING`[[:space:]]*\|[[:space:]]*$' "$GEN_ROOT/docs/REVIEWER_ASSURANCE.md" 2>/dev/null; then
    fail "reviewer-assurance: at least one register row is BLOCKING (Task 9 must keep every prompt reviewer at SHADOW; Task 10 records the baseline)"
else
    pass "reviewer-assurance: no register row currently BLOCKING"
fi
# Every prompt-reviewer row is present AND marked SHADOW.
for reviewer in red-team sast-reviewer security-hardener \
        agent-security-specialist code-reviewer solution-architect \
        test-architect data-flow-guardian; do
    if grep -qE "^\|[[:space:]]*${reviewer}.*\`SHADOW\`[[:space:]]*\|" "$GEN_ROOT/docs/REVIEWER_ASSURANCE.md" 2>/dev/null; then
        pass "reviewer-assurance: ${reviewer} row present and SHADOW"
    else
        fail "reviewer-assurance: ${reviewer} row missing or not SHADOW"
    fi
done
# INDEX links to the assurance register.
has "reviewer-assurance: INDEX links the assurance register" \
    'REVIEWER_ASSURANCE\.md' docs/INDEX.md
# DEVELOPMENT_PROCESS references the assurance contract and the
# SHADOW-reset invariant so a maintainer sees the gate from the
# process doc alone.
has "reviewer-assurance: process doc references the assurance register" \
    'REVIEWER_ASSURANCE\.md' docs/DEVELOPMENT_PROCESS.md
has "reviewer-assurance: process doc names SHADOW-reset invariant" \
    'returns the row to `SHADOW`' docs/DEVELOPMENT_PROCESS.md
# GOVERNANCE Non-Claim: manual prompt-reviewer results are point-in-time
# evidence, not CI automation, not proof against unknown attacks.
has "reviewer-assurance: GOVERNANCE Non-Claim names manual prompt-reviewer point-in-time" \
    '[Mm]anual [Pp]rompt-[Rr]eviewer' docs/GOVERNANCE.md
has "reviewer-assurance: GOVERNANCE Non-Claim states point-in-time" \
    'point-in-time' docs/GOVERNANCE.md
has "reviewer-assurance: GOVERNANCE Non-Claim disclaims unknown-attack proof" \
    'not proof against unknown attacks' docs/GOVERNANCE.md
# The always-applied red-team rule and expert-review protocol both point
# at the assurance register. These are the always-applied gates that
# actually consult REVIEWER_ASSURANCE.md at review time.
has "reviewer-assurance: red-team rule references assurance register" \
    'REVIEWER_ASSURANCE\.md' .cursor/rules/red-team.mdc
has "reviewer-assurance: expert-review rule references assurance register" \
    'REVIEWER_ASSURANCE\.md' .cursor/rules/expert-review.mdc

# Open adversarial corpus + provenance ship in EVERY profile (tests/ is not
# gated by include_evals). The fixture is plain .py copier never renders, so a
# raw jinja sequence would survive to runtime -- assert there is none, and that
# the source stayed pure ASCII (non-ASCII must be \uXXXX-escaped so the seed
# hashes cannot drift under editor/OS renormalization).
exists "open corpus: fixture ships in all profiles" tests/adversarial_payloads_open.py
exists "open corpus: provenance manifest ships in all profiles" tests/fixtures/provenance.json
exists "open corpus: tests/fixtures ATTRIBUTION ships in all profiles" tests/fixtures/ATTRIBUTION.md
lacks "open corpus: fixture has no raw jinja expression braces" '\{\{' tests/adversarial_payloads_open.py
lacks "open corpus: fixture has no raw jinja statement braces" '\{%' tests/adversarial_payloads_open.py
# Portable ASCII check: BSD grep has no -P (it exits 2, which a naive
# grep -qP pipeline silently mistakes for "no match"), so decode with
# python3 -- already a hard dependency of this script.
if python3 -c "import sys; open(sys.argv[1], encoding='ascii').read()" \
        "$GEN_ROOT/tests/adversarial_payloads_open.py" 2>/dev/null; then
    pass "open corpus: fixture source is pure ASCII"
else
    fail "open corpus: fixture source is not pure ASCII (non-ASCII must be \\uXXXX-escaped)"
fi
# OWASP mapping is an unconditional doc; evals-gated artifacts it cites carry a
# static "(requires include_evals)" annotation rather than being omitted.
exists "OWASP map: docs/SECURITY_MAPPING.md ships in all profiles" docs/SECURITY_MAPPING.md
has "OWASP map: cites LLM01 Prompt Injection" 'LLM01' docs/SECURITY_MAPPING.md
has "OWASP map: cites Agentic ASI category" 'ASI0' docs/SECURITY_MAPPING.md
# Pin the published ASI labels (2026 list) so silent label drift is caught,
# including the two binding honest-gap rows (ASI05 not covered, ASI07 N/A).
has "OWASP map: ASI05 is the published Unexpected Code Execution row" 'ASI05 \| Unexpected Code Execution' docs/SECURITY_MAPPING.md
has "OWASP map: ASI05 claims 'Not covered'" 'ASI05 \| Unexpected Code Execution \| \*\*Not covered' docs/SECURITY_MAPPING.md
has "OWASP map: ASI07 is the published Inter-Agent Communication row" 'ASI07 \| Insecure Inter-Agent Communication' docs/SECURITY_MAPPING.md
has "OWASP map: ASI07 claims N/A by architecture" 'ASI07 \| Insecure Inter-Agent Communication \| \*\*Not applicable' docs/SECURITY_MAPPING.md
has "OWASP map: ASI06 is the published Memory and Context Poisoning row" 'ASI06 \| Memory and Context Poisoning' docs/SECURITY_MAPPING.md
has "OWASP map: annotates evals-gated rows" 'requires .include_evals.' docs/SECURITY_MAPPING.md
# Retrieval ranking ships in every profile (learning modules are ungated);
# the GOVERNANCE row and its kill switch must always be documented.
has "GOVERNANCE: lexical/hybrid retrieval ranking row" 'Lexical . hybrid retrieval ranking' docs/GOVERNANCE.md
has "GOVERNANCE: retrieval kill switch documented" 'LEXICAL_RANKING_ENABLED=false' docs/GOVERNANCE.md

# Corrections validity + supersession (B7) also ships in every profile
# (learning modules are ungated). Pair the capability with honesty
# assertions -- no positive "point-in-time" claim, legacy still current,
# update_if required, no auto-invalidate.
has "GOVERNANCE: corrections validity / supersession row" \
    'human-gated supersession' docs/GOVERNANCE.md
has "GOVERNANCE: no auto-invalidate Non-Claim" \
    'no auto-invalidat' docs/GOVERNANCE.md
has "GOVERNANCE: legacy rows remain currently valid" \
    'still currently valid' docs/GOVERNANCE.md
has "GOVERNANCE: supersession requires update_if" \
    'update_if' docs/GOVERNANCE.md
lacks "GOVERNANCE: no positive point-in-time capability claim" \
    'point-in-time (query|API|capability)' docs/GOVERNANCE.md
has "PLATFORM_GUIDE: /supersede API row" \
    '/corrections/.*/supersede' docs/PLATFORM_GUIDE.md
has "PLATFORM_GUIDE: /revalidate API row" \
    '/corrections/.*/revalidate' docs/PLATFORM_GUIDE.md
has "PLATFORM_GUIDE: stale default behavior change note" \
    'Behavior change for upgraders' docs/PLATFORM_GUIDE.md
# A5 -- approval-health (human-gate health check): capability + Non-Claim
# + PLATFORM_GUIDE bullet; never claim we "detect rubber-stamping".
has "GOVERNANCE: approval-health human-gate row" \
    'human-gate health check' docs/GOVERNANCE.md
has "GOVERNANCE: not a rubber-stamping detector Non-Claim" \
    'not a rubber-stamping detector' docs/GOVERNANCE.md
has "PLATFORM_GUIDE: approval-health human gate bullet" \
    'health check on the human gate' docs/PLATFORM_GUIDE.md
lacks "GOVERNANCE: no detects-rubber-stamping claim" \
    'detects rubber-stamping' docs/GOVERNANCE.md
lacks "GOVERNANCE: no collusion-score claim" \
    'collusion score' docs/GOVERNANCE.md
# B1 -- governed procedures (typed corrections): capability + Non-Claim +
# separate-resource recipe; Extraction guard names procedures; no
# procedure/extraction-playbook conflation on the Governed procedures row.
has "GOVERNANCE: governed procedures capability row" \
    'Governed procedures \(typed corrections\)' docs/GOVERNANCE.md
has "GOVERNANCE: procedure type framing" \
    'type=procedure' docs/GOVERNANCE.md
has "GOVERNANCE: Extraction guard names procedures" \
    'and procedures listings' docs/GOVERNANCE.md
has "GOVERNANCE: approval does not auto-ground procedures" \
    'Approving a procedure does not automatically ground' docs/GOVERNANCE.md
has "PLATFORM_GUIDE: /api/v1/procedures route" \
    '/api/v1/procedures' docs/PLATFORM_GUIDE.md
has "PLATFORM_GUIDE: procedures separate resource recipe" \
    'Procedures are a separate resource' docs/PLATFORM_GUIDE.md
# Adjacency: Governed-procedures capability must not confuse with Sequence
# monitoring's extraction-playbook language (that phrase may still appear
# elsewhere in GOVERNANCE.md).
if grep -iE 'Governed procedures.*extraction playbook|extraction playbook.*Governed procedures' docs/GOVERNANCE.md >/dev/null 2>&1; then
    fail "GOVERNANCE: Governed procedures row must not conflate with extraction playbook"
else
    pass "GOVERNANCE: no procedure/extraction-playbook conflation on capability row"
fi

if [ "$INCLUDE_EVALS" = "true" ]; then
    exists "evals on: red-team config present" evals/redteam/redteam.yaml
    exists "evals on: red-team README present" evals/redteam/README.md
    exists "evals on: evals/fixtures ATTRIBUTION present" evals/fixtures/ATTRIBUTION.md
    # promptfoo's own {{prompt}} variable must survive copier rendering intact
    # (it is wrapped in {% raw %}), and the data-egress warning must be present.
    has "evals on: red-team config keeps literal promptfoo {{prompt}} var" '\{\{prompt\}\}' evals/redteam/redteam.yaml
    has "evals on: red-team config carries the data-egress warning" 'DATA EGRESS' evals/redteam/redteam.yaml
    has "evals on: golden set resolves the open corpus" 'corpus_open' evals/tasks/test_injection_defense_golden.py
    exists "evals on: public corpus manifest" evals/fixtures/public_corpus_manifest.json
    exists "evals on: public corpus cases" evals/fixtures/public_corpus_cases.json
    exists "evals on: public corpus baseline" evals/fixtures/public_corpus_baseline.json
    exists "evals on: public corpus harness" evals/tasks/test_public_corpus_harness.py
    exists "evals on: public corpus resolve helper" evals/tasks/corpus_resolve.py
    lacks "evals on: public corpus cases lack jinja open braces" '\{\{' evals/fixtures/public_corpus_cases.json
    lacks "evals on: public corpus manifest lack jinja open braces" '\{\{' evals/fixtures/public_corpus_manifest.json
    lacks "evals on: public corpus baseline lack jinja open braces" '\{\{' evals/fixtures/public_corpus_baseline.json
    # ASCII parity with open-corpus fixtures (defense in depth if Step 9b skipped).
    if python3 -c "import pathlib,sys; [pathlib.Path(p).read_text(encoding='ascii') for p in sys.argv[1:]]" \
        "$GEN_ROOT/evals/fixtures/public_corpus_cases.json" \
        "$GEN_ROOT/evals/fixtures/public_corpus_manifest.json" \
        "$GEN_ROOT/evals/fixtures/public_corpus_baseline.json" 2>/dev/null; then
        pass "evals on: public corpus JSON fixtures are ASCII"
    else
        fail "evals on: public corpus JSON fixtures must be ASCII-encodable"
    fi
    has "evals on: ATTRIBUTION cites InjecAgent" 'InjecAgent' evals/fixtures/ATTRIBUTION.md
    has "evals on: ATTRIBUTION cites AgentDojo" 'AgentDojo' evals/fixtures/ATTRIBUTION.md
else
    # (Step 11 already asserts evals/ is excluded entirely -- not repeated here.)
    absent "evals off: red-team config excluded" evals/redteam/redteam.yaml
    absent "evals off: public corpus manifest excluded" evals/fixtures/public_corpus_manifest.json
    absent "evals off: public corpus cases excluded" evals/fixtures/public_corpus_cases.json
    absent "evals off: public corpus baseline excluded" evals/fixtures/public_corpus_baseline.json
    absent "evals off: public corpus harness excluded" evals/tasks/test_public_corpus_harness.py
    absent "evals off: public corpus resolve helper excluded" evals/tasks/corpus_resolve.py
fi

# Public-corpus docs claims (ship in all profiles; must not overclaim)
has "SECURITY_MAPPING: pinned subsets phrasing" 'pinned subsets' docs/SECURITY_MAPPING.md
has "SECURITY_MAPPING: public corpus requires include_evals" 'requires .include_evals.' docs/SECURITY_MAPPING.md
lacks "SECURITY_MAPPING: no leaderboard claim" 'leaderboard' docs/SECURITY_MAPPING.md
lacks "SECURITY_MAPPING: no SOTA claim" 'SOTA' docs/SECURITY_MAPPING.md
lacks "SECURITY_MAPPING: no AgentDojo score claim" 'AgentDojo score' docs/SECURITY_MAPPING.md
lacks "SECURITY_MAPPING: no beats AgentDojo claim" 'beats AgentDojo' docs/SECURITY_MAPPING.md
lacks "GOVERNANCE: no beats AgentDojo claim" 'beats AgentDojo' docs/GOVERNANCE.md
has "SECURITY_MAPPING: ASI05 Not covered" 'ASI05 .*Not covered' docs/SECURITY_MAPPING.md
has "SECURITY_MAPPING: ASI07 Not applicable" 'ASI07 .*Not applicable' docs/SECURITY_MAPPING.md

# Refresh helper must stay at repo root (never under generated project scripts/)
if [ -f "$GEN_ROOT/scripts/refresh_public_corpus.py" ]; then
    fail "refresh_public_corpus.py must not ship into generated projects"
else
    pass "refresh helper absent from generated project scripts/"
fi

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
    # The context-pressure signal is wired in the LLM client (active in
    # every profile), so its posture row -- carrying the no-[metrics]
    # blind-spot note -- must render with the gateway off too. Pattern is
    # the row's header cell, which appears ONLY in the posture row (the
    # monitor table and triage section mention the flag in other phrasings
    # and would keep a bare-flag grep green if the row regressed into the
    # gateway-only branch).
    has "gateway off: OPERATIONS.md documents context-pressure posture" 'Context-pressure signal \(.CONTEXT_PRESSURE_ENABLED.\) \| Flag unset' docs/OPERATIONS.md
    # The loop-integrity chat call site is core (chat_helpers.py, every
    # profile), so its posture row -- carrying the no-metric discovery
    # note -- must render with the gateway off too. Two assertions: the
    # posture-row header cell, and the no-metric note (which must not
    # regress into a gateway-only branch).
    has "gateway off: OPERATIONS.md documents loop-integrity posture" 'Loop-integrity detection \(.LOOP_INTEGRITY_DETECTION_ENABLED.\) \| Flag unset' docs/OPERATIONS.md
    has "gateway off: loop-integrity no-metric note renders" 'No metric -- discover findings via the store' docs/OPERATIONS.md
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
# Step 12: Dependency Audit (pip-audit through the fail-closed gate)
# =========================================================================
# Every profile audits its rendered requirements set via
# ``pip-audit -r requirements.txt`` (resolved from PyPI metadata, no full
# install). The FULL profile additionally installs every rendered
# optional extra into a throwaway venv and audits the resolved
# environment, matching what the generated ``security`` CI job does. Any
# other profile's extras subset is a strict subset of full's, so
# auditing full once covers the cross product without multiplying venv
# cost by four.
#
# Network failures (PyPI/OSV unreachable) fail this step. Do not add
# ``|| true``, ``continue-on-error``, or ``--fix`` here; a transient
# failure is a job to rerun, not a soft pass. Local runs must have
# outbound access; CI runners already do.
section "Step 12: Dependency Audit (pip-audit through gate)"
# Both preconditions below are contract, not opportunistic: the
# validation venv contract pins ``pip-audit==2.10.1``, and rendered
# projects always emit ``requirements.txt``. A missing tool or missing
# rendered file means the environment or the generator regressed, so
# fail rather than emit a warning that a reviewer could miss --
# validate_generated.sh is the gate that must pass before any code
# review, and a silent skip here would let unaudited requirements
# reach review.
if ! command -v pip-audit &>/dev/null; then
    fail "pip-audit not on PATH: the validation venv contract pins pip-audit==2.10.1; a missing tool is an environment regression, not a soft skip"
elif [ ! -f "$GEN_ROOT/requirements.txt" ]; then
    fail "pip-audit: requirements.txt missing in generated project at $GEN_ROOT/requirements.txt: a rendered project always emits requirements.txt, so this is a generator regression, not a soft skip"
elif [ ! -f "$GEN_ROOT/scripts/pip_audit_gate.py" ]; then
    fail "pip-audit gate script missing at scripts/pip_audit_gate.py"
else
    AUDIT_LOG="$TEST_DIR/pip_audit_${PROFILE}.log"
    (cd "$GEN_ROOT" && python3 scripts/pip_audit_gate.py \
        --exceptions .github/pip-audit-exceptions.json \
        -r requirements.txt) >"$AUDIT_LOG" 2>&1
    AUDIT_STATUS=$?
    if [ "$AUDIT_STATUS" -eq 0 ]; then
        pass "pip-audit: rendered requirements.txt clean (profile=$PROFILE)"
    else
        fail "pip-audit: findings or auditor error against requirements.txt (exit $AUDIT_STATUS)"
        tail -40 "$AUDIT_LOG" | sed 's/^/    /'
    fi

    if [ "$PROFILE" = "full" ]; then
        FULL_VENV="$TEST_DIR/audit-full-venv"
        FULL_LOG="$TEST_DIR/pip_audit_full_env.log"
        : >"$FULL_LOG"
        if python3 -m venv "$FULL_VENV" >>"$FULL_LOG" 2>&1 \
            && "$FULL_VENV/bin/pip" install --upgrade --quiet pip >>"$FULL_LOG" 2>&1 \
            && "$FULL_VENV/bin/pip" install --quiet pip-audit==2.10.1 >>"$FULL_LOG" 2>&1 \
            && "$FULL_VENV/bin/pip" install --quiet -r "$GEN_ROOT/requirements.txt" >>"$FULL_LOG" 2>&1 \
            && (cd "$GEN_ROOT" && "$FULL_VENV/bin/pip" install --quiet ".[postgres,mcp,metrics,otel,load]" >>"$FULL_LOG" 2>&1); then
            (cd "$GEN_ROOT" && "$FULL_VENV/bin/python" scripts/pip_audit_gate.py \
                --exceptions .github/pip-audit-exceptions.json) >>"$FULL_LOG" 2>&1
            FULL_STATUS=$?
            if [ "$FULL_STATUS" -eq 0 ]; then
                pass "pip-audit (full profile): extras-installed environment clean"
            else
                fail "pip-audit (full profile): findings or auditor error against extras (exit $FULL_STATUS)"
                tail -40 "$FULL_LOG" | sed 's/^/    /'
            fi
        else
            fail "pip-audit (full profile): venv/install setup failed (see log below)"
            tail -40 "$FULL_LOG" | sed 's/^/    /'
        fi
    fi
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
