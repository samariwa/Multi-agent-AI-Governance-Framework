.PHONY: up down test test-a

up:
	docker compose up -d --build

down:
	docker compose down

.venv:
	python3 -m venv .venv
	.venv/bin/pip install -q -r tests/requirements.txt
	.venv/bin/playwright install chromium

test: .venv
	.venv/bin/pytest tests -v

test-a: .venv
	.venv/bin/pytest tests/test_phase_a.py -v
