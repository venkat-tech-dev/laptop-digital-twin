all: check

# Windows users: the PowerShell scripts in scripts/ do the same without make.

infra:
	docker compose up -d postgres redis

migrate:
	cd backend && .venv/Scripts/alembic upgrade head

backend:
	cd backend && .venv/Scripts/python -m app.main

agent:
	cd agent && .venv/Scripts/python -m app.main

discover:
	cd agent && .venv/Scripts/python -m app.main --discover

frontend:
	cd frontend && npm run dev

stack:
	docker compose up -d --build

check:
	cd agent && .venv/Scripts/ruff check app tests && .venv/Scripts/mypy app && .venv/Scripts/python -m pytest -q
	cd backend && .venv/Scripts/ruff check app tests && .venv/Scripts/mypy app && .venv/Scripts/python -m pytest -q
	cd frontend && npx oxlint src && npx vitest run && npm run build

.PHONY: all infra migrate backend agent discover frontend stack check
