# aiscaffold Development Makefile
# Validation pipeline for the scaffold template itself.
#
# Three layers of enforcement:
#   1. make quick     -- Fast checks on templates (~5s)
#   2. make validate  -- All generation profiles for the default config
#   3. CI (GitHub Actions) -- Adds two more configs (full profile each)
#
# Run 'make help' to see all targets.

.PHONY: help quick validate validate-matrix fix clean hooks-install

TEMPLATE_DIR := template/{{project_slug}}
TEMPLATE_SRC := $(TEMPLATE_DIR)/src/{{project_slug}}
TEST_PROJECT_DIR := /tmp/aiscaffold_test_project
PYTHON := python3

help: ## Show all available targets
	@echo "aiscaffold Development Commands"
	@echo "================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

hooks-install: ## Point this clone at .githooks (review-receipt pre-commit)
	@chmod +x .githooks/pre-commit
	@git config core.hooksPath .githooks
	@echo "core.hooksPath=.githooks (native review-receipt pre-commit enabled)"
	@echo "Note: chains to Python pre-commit framework when that CLI is installed."

# =============================================================================
# FAST CHECKS (~5 seconds, run on templates directly)
# =============================================================================

quick: ## Fast checks on templates: banned patterns, file sizes, secrets, Jinja syntax
	@echo "=== Quick Validation (templates only) ==="
	@$(PYTHON) scripts/quick_checks.py
	@echo ""
	@echo "\033[32m✓ Quick checks passed\033[0m"

# =============================================================================
# FULL VALIDATION (~30 seconds, generates test project)
# =============================================================================

validate: ## Full validation: all generation profiles (full/gateway-off/minimal/defaults) for the default config
	@echo "=== Full Validation (all generation profiles) ==="
	@bash scripts/validate_generated.sh "web-app" "anthropic" "sqlite" "all"
	@echo ""
	@echo "\033[32m✓ Full validation passed\033[0m"

validate-matrix: ## Matrix validation: all profiles for the default config + full profile for 2 more configs
	@echo "=== Matrix Validation ==="
	@echo ""
	@echo "--- Config 1/3: web-app + anthropic + sqlite (all profiles) ---"
	@bash scripts/validate_generated.sh "web-app" "anthropic" "sqlite" "all"
	@echo ""
	@echo "--- Config 2/3: multi-agent + openai + postgres (full profile) ---"
	@bash scripts/validate_generated.sh "multi-agent" "openai" "postgres" "full"
	@echo ""
	@echo "--- Config 3/3: api-service + anthropic + sqlite (full profile) ---"
	@bash scripts/validate_generated.sh "api-service" "anthropic" "sqlite" "full"
	@echo ""
	@echo "\033[32m✓ All 3 configurations passed\033[0m"

# =============================================================================
# AUTO-FIX
# =============================================================================

fix: ## Auto-fix formatting in template Python files
	@echo "=== Auto-fix ==="
	@find $(TEMPLATE_SRC) -name "*.py" -not -path "*/__pycache__/*" | xargs black --quiet 2>/dev/null || echo "black not installed -- skipping format"
	@find $(TEMPLATE_SRC) -name "*.py" -not -path "*/__pycache__/*" | xargs ruff check --fix --quiet 2>/dev/null || echo "ruff not installed -- skipping lint fix"
	@echo "\033[32m✓ Auto-fix complete\033[0m"

# =============================================================================
# INDIVIDUAL CHECKS (used by validate_generated.sh)
# =============================================================================

ai-checks: ## Run AI-specific validation on generated project
	@$(PYTHON) scripts/ai_checks.py $(TEST_PROJECT_DIR)

agent-review: ## Run automated agent review checklist on generated project
	@$(PYTHON) scripts/agent_review.py $(TEST_PROJECT_DIR)

# =============================================================================
# CLEANUP
# =============================================================================

clean: ## Remove test projects and caches
	@rm -rf /tmp/aiscaffold_test_*
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."
