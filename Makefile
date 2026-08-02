PYTHON ?= python3

.PHONY: contract contract-check

# Emit the fifth contract artifact, docs/llms.txt (stapel_tools.llms_txt — the
# module's own context slice for an agent; badge-canon §3), from the committed
# docs/capabilities.json. NOTE: docs/capabilities.json in this module is
# HAND-AUTHORED (no schema/flows/errors triad emitter exists — see git log:
# "author capabilities.json for the stapel-catalog sweep") and this target
# NEVER writes it — only docs/llms.txt.
contract:
	$(PYTHON) -m stapel_tools.llms_txt . --out docs

# Drift gate: llms_txt's own --check mode compares a fresh render (from the
# committed docs/capabilities.json) against the committed docs/llms.txt.
contract-check:
	$(PYTHON) -m stapel_tools.llms_txt . --check

.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
