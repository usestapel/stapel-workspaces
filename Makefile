# stapel-workspaces — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its OWN contract triad (schema.json + flows.json + errors.json)
# per-module, byte-identical to the monolith aggregate's workspaces slice, from a
# single-module {workspaces + core} Django instance mounted at the canonical
# /workspaces/api/ prefix (see _codegen.py / _codegen_settings.py / codegen_urls.py).
#
# PYTHON must have the module + its deps importable (the workspace venv, or a CI
# venv). The authoritative CI gate is tests/test_contract.py (run under pytest);
# these targets are the dev-loop convenience.
PYTHON ?= python3

.PHONY: contract contract-check

# Emit the contract triad + capabilities.json + llms.txt (the fifth contract
# artifact, stapel_tools.llms_txt) into docs/.
#
# The mandate-model surface section (permissions.py + capabilities.py +
# services.py — 39 entries: guest predicate, rank-guard, invitation/provision/
# suspension primitives, and since 0.19 the three-symbol profiles seam behind
# the roster's name edit) does not fit the generator's default 4000-token
# budget (~4835 tokens at honest intent length). The owner's call, same
# exception stapel-auth already takes: raise the ceiling for this module
# rather than shorten intents to fit — a trimmed-to-fit context file is
# indistinguishable from a complete one at the point of use, which is the
# failure mode the hard-budget gate exists to prevent. Raised 4500 -> 5000 in
# 0.19, and 5000 -> 5500 in 0.20, for the same reason it was raised the first
# time: the surface grew (0.20 adds the per-user preferred-workspace pair —
# the explicit choice DEFAULT_WORKSPACE_ID documents itself as yielding to),
# and the honest description of it grew with it; 5500 -> 6000 with the
# audit journal's move into the core event store (the sink seam, the
# anchor read and the migration data path each earned a surface entry).
# contract-check below enforces the same ceiling; it does not disable the
# check.
#
# README.md is the SIXTH artifact (tracker #257): assembled by
# stapel_tools.readme from docs/readme.md (the human half — what this module
# is, how to think about it) plus everything emitted above. Badges, version,
# surface counts and doc links are generated, so a release cannot leave them
# behind. Edit docs/readme.md; never README.md.
contract:
	$(PYTHON) -m stapel_workspaces._codegen --out docs
	$(PYTHON) -m stapel_workspaces._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 6000
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.json
# (mirrors the monolith's `make codegen-check` and the frontend's `gen:*:check`).
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_workspaces._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_workspaces._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget 6000 || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc


.PHONY: migration-lint

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict
