PYTHON ?= python3

.PHONY: contract contract-check

# stapel-listings — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its own contract triad (schema.json + flows.json +
# errors.json) from a single-module {listings + core} Django instance mounted
# at the canonical /listings/api/v1 prefix (see _codegen.py /
# _codegen_settings.py / codegen_urls.py) — the same mechanism stapel-search,
# stapel-chat and stapel-forms already use. Emission is pinned to Python
# 3.12: drf-spectacular renders component descriptions differently across
# minors, and a contract emitted on the wrong one produces false diffs
# forever.
#
# First: the triad itself.
#
# Second: the `surface` section of docs/capabilities.json — the symbols a
# product is meant to CALL (discoverability-design.md §1.2). Entries are
# derived by AST from the roots declared in docs/capabilities.meta.json; a
# selected export with no curated intent line fails this target naming the
# symbol.
#
# NOTE the rest of docs/capabilities.json is still HAND-AUTHORED (see git
# log: "author capabilities.json for the stapel-catalog sweep") — `--patch`
# refreshes only the derivable parts: module/version and `surface`, leaving
# the rest verbatim.
#
# Third: docs/llms.txt, the fifth contract artifact (stapel_tools.llms_txt —
# the module's own context slice for an agent; badge-canon §3), rendered from
# docs/capabilities.json AND the triad above (llms_txt picks up
# schema/errors/flows automatically when present).
#
# Fourth: assemble README.md (stapel_tools.readme) from docs/readme.md — the
# human half, the only file a person edits — plus the artifacts above. The
# badge row, the version, the surface counts and every doc link are generated,
# so they cannot lag a release the way a hand-written README always has.
contract:
	$(PYTHON) -m stapel_listings._codegen --out docs
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt . --out docs
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate the triad into a temp dir and diff against the
# committed docs/*, then run the existing surface/llms.txt/README checks.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_listings._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.surface . --patch --check || rc=1; \
	$(PYTHON) -m stapel_tools.llms_txt . --check || rc=1; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc

.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
