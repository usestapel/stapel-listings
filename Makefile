PYTHON ?= python3

.PHONY: contract contract-check

# First: the `surface` section of docs/capabilities.json — the symbols a
# product is meant to CALL (discoverability-design.md §1.2). Entries are
# derived by AST from the roots declared in docs/capabilities.meta.json; a
# selected export with no curated intent line fails this target naming the
# symbol.
#
# NOTE the rest of docs/capabilities.json is still HAND-AUTHORED (no
# schema/flows/errors triad emitter exists — see git log: "author
# capabilities.json for the stapel-catalog sweep") — `--patch` refreshes only
# the derivable parts: module/version and `surface`, leaving the rest verbatim.
#
# Second: docs/llms.txt, the fifth contract artifact (stapel_tools.llms_txt —
# the module's own context slice for an agent; badge-canon §3), rendered
# straight from the docs/capabilities.json the step above produces.
contract:
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt . --out docs

# Drift gate: surface --patch --check compares the derived surface + refreshed
# module/version against the committed docs/capabilities.json; llms_txt's own
# --check mode compares a fresh render against the committed docs/llms.txt.
contract-check:
	$(PYTHON) -m stapel_tools.surface . --patch --check
	$(PYTHON) -m stapel_tools.llms_txt . --check

.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
