UV ?= uv
DIRECTORY ?= /mnt/bigssd/tickyticker/data
SETTINGS ?= $(CURDIR)/src/tickytickertextual/defaults.toml
LOCK_FILE ?= /tmp/tickyticker/tickytickertextual.lock
PRODUCTION ?= 0
HOST ?= 0.0.0.0
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
	$(UV) run tickytickertextual $(DIRECTORY) --settings $(SETTINGS) --lock-file $(LOCK_FILE) $(if $(filter 1,$(PRODUCTION)),--production,)

web:
	$(UV) run tickytickertextual-web $(DIRECTORY) --settings $(SETTINGS) --lock-file $(LOCK_FILE) --host $(HOST) --port $(PORT) $(if $(PUBLIC_URL),--public-url $(PUBLIC_URL),) $(if $(filter 1,$(PRODUCTION)),--production,)
