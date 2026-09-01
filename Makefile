UV ?= uv
DIRECTORY ?=
HOST ?= 127.0.0.1
PORT ?= 8000
PUBLIC_URL ?=

.PHONY: venv sync test run web

venv:
	$(UV) venv .venv

sync:
	$(UV) sync --extra dev

test:
	$(UV) run pytest -q

run:
	$(UV) run tickytickertextual $(DIRECTORY)

web:
	$(UV) run tickytickertextual-web $(DIRECTORY) --host $(HOST) --port $(PORT) $(if $(PUBLIC_URL),--public-url $(PUBLIC_URL),)
