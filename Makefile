.PHONY: test lint fmt run cli live doctor simulate

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

run:
	uv run uarb-agent serve

# usage: make cli ARGS='fetch M12205 "Other Documents"'
cli:
	uv run uarb-agent $(ARGS)

# hits the real UARB site; keep it rare
live:
	uv run pytest -m live tests/live

# live canary: one session on M12205, one download
doctor:
	uv run uarb-agent doctor

# full pipeline on a made-up email, nothing sent: make simulate BODY='Other Documents from M12205'
simulate:
	uv run uarb-agent simulate --from "you@example.com" --subject "$(or $(SUBJECT),Documents)" --body "$(BODY)"
