# MusicTidy server — Makefile for self-hosters and contributors

SHELL := /bin/bash

.PHONY: help
help:
	@echo "MusicTidy server — make targets:"
	@echo ""
	@echo "  make dev              start dev server with auto-reload"
	@echo "  make test             run pytest"
	@echo "  make lint             run ruff"
	@echo ""
	@echo "  make site-install     install Astro deps for site/"
	@echo "  make site-dev         serve site/ on http://localhost:4321"
	@echo "  make site-build       build site/ to site/dist/"
	@echo ""
	@echo "See https://musictidy.com/deploy for self-host instructions."
	@echo ""

# ─── server ──────────────────────────────────────────────────
.PHONY: dev
dev:
	@cd server && .venv/bin/python -m app.main

.PHONY: test
test:
	@cd server && .venv/bin/python -m pytest tests/ -q

.PHONY: lint
lint:
	@cd server && .venv/bin/ruff check app/ tests/

# ─── site (Astro) ────────────────────────────────────────────
.PHONY: site-install
site-install:
	@cd site && npm install

.PHONY: site-dev
site-dev:
	@cd site && npm run dev

.PHONY: site-build
site-build:
	@cd site && npm run build
