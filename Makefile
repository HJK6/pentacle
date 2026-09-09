# Pentacle developer targets.

.PHONY: hooks

# Enable the opt-in local pre-push gate (tools/git-hooks/pre-push).
# Idempotent; refuses if core.hooksPath already points elsewhere.
# See docs/developer_onboarding.md § Tests and gates.
hooks:
	@current=$$(git config --get core.hooksPath || true); \
	if [ -n "$$current" ] && [ "$$current" != "tools/git-hooks" ]; then \
	  echo "core.hooksPath is already set to '$$current'." >&2; \
	  echo "Refusing to override. To chain, call tools/git-hooks/pre-push from that directory's" >&2; \
	  echo "pre-push, or run: git config --unset core.hooksPath && make hooks" >&2; \
	  exit 1; \
	fi; \
	git config core.hooksPath tools/git-hooks; \
	echo "core.hooksPath -> tools/git-hooks (pre-push local gate enabled)"
