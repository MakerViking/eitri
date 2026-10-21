# SPDX-License-Identifier: Apache-2.0
# Eitri: development entry points. Nothing here touches a printer.
#
#   make help
#   make test            every component's tests (about 5 minutes, most of it klippy)
#   make test-fast       the same without the klippy integration tests
#   make lint            syntax gate over every script, module and data file
#   make seed            build both data seeds (klipper, kalico) and verify them under qemu-aarch64 (network, uv, zstd)
#   make test-seed       the data seed's own tests (offline; they skip what needs built seeds)
#   make os-defconfig    configure Buildroot for the OS image (does not build it)
#   make configurator-dev  run the setup wizard against fake MCUs and a fake Moonraker
#
# The workspace layout is <workspace>/eitri (this repository), <workspace>/build (scratch,
# downloads, virtual environments: untrusted data, never inside the repository) and
# <workspace>/src (upstream Klipper checkout used by the klipper-layer tests). Override with
# WORKSPACE=..., or point single tools at other places with the variables below.

SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:

ROOT      := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
WORKSPACE ?= $(abspath $(ROOT)/..)
BUILD     ?= $(WORKSPACE)/build

PY            ?= python3
# Klarity needs pyserial-asyncio-fast, websockets and PyYAML; the klipper-layer tests need klippy's
# dependencies (cffi, greenlet, jinja2, pyserial). The workspace venvs have them; fall back to PY.
KLARITY_PY    ?= $(firstword $(wildcard $(BUILD)/klarity/venv/bin/python) $(PY))
KLIPPER_PY    ?= $(firstword $(wildcard $(BUILD)/klipper-layer/venv/bin/python) $(PY))
# Host tools the OS tests use when they exist (a finished or partial Buildroot build).
OS_HOST_BIN   ?= $(BUILD)/br-out/host/bin
PORT          ?= 8099

.PHONY: help test test-fast test-repo test-klarity test-configurator test-camera test-installer \
	test-os test-klipper-layer test-klipper-layer-fast test-seed test-tools seed lint check-repo os-defconfig os-check \
	configurator-dev clean

help:
	@sed -n '3,9p' $(lastword $(MAKEFILE_LIST)) | sed 's/^# \{0,1\}//'
	@echo
	@echo "  per component: test-repo test-klarity test-configurator test-camera test-installer"
	@echo "                 test-os test-klipper-layer (test-klipper-layer-fast skips klippy)"
	@echo "                 test-seed (the data seed's tests) test-tools (release tooling)"
	@echo "  seed           build and verify the data seeds into $(BUILD)/seed/out (needs network)"
	@echo "  os-check       pins, defconfig and unit tests of the OS tree (needs the Buildroot checkout)"
	@echo "  clean          remove byte-code and test caches"

# --- tests ------------------------------------------------------------------------------------------
test: test-repo test-tools test-klarity test-configurator test-camera test-installer test-seed test-os test-klipper-layer
	@echo; echo "make test: all component suites passed"

test-fast: test-repo test-tools test-klarity test-configurator test-camera test-installer test-seed test-os test-klipper-layer-fast
	@echo; echo "make test-fast: all component suites passed (klippy integration tests skipped)"

test-repo: lint
	$(PY) -I $(ROOT)/tools/check-repo.py

test-tools:
	$(PY) -m pytest $(ROOT)/tools/tests -q -p no:cacheprovider

test-seed:
	$(MAKE) -C $(ROOT)/seed seed BUILD_ROOT=$(BUILD)

test-klarity:
	cd $(ROOT)/klarity && $(KLARITY_PY) -W error -m pytest tests -q -p no:cacheprovider

test-configurator:
	cd $(ROOT)/configurator && $(PY) -m pytest -q -p no:cacheprovider

test-camera:
	sh $(ROOT)/camera/tests/run-tests.sh

test-installer:
	cd $(ROOT)/installer && ./run-tests.sh -p no:cacheprovider

test-os:
	$(PY) -I $(ROOT)/os/scripts/check-pins.py
	cd $(ROOT)/os && \
	EITRI_GENIMAGE="$${EITRI_GENIMAGE:-$(wildcard $(OS_HOST_BIN)/genimage)}" \
	EITRI_RAUC="$${EITRI_RAUC:-$(wildcard $(OS_HOST_BIN)/rauc)}" \
	$(PY) -m pytest -q -p no:cacheprovider tests

# Builds Klipper and Kalico dictionaries once (tests/build-dicts.sh, needs arm-none-eabi-gcc) and runs
# the real klippy in batch mode; without them those tests skip with a reason.
test-klipper-layer:
	cd $(ROOT)/klipper-layer && $(KLIPPER_PY) -B -m pytest -q

test-klipper-layer-fast:
	cd $(ROOT)/klipper-layer && $(KLIPPER_PY) -B -m pytest -q --ignore=tests/test_klippy_batch.py

# Builds and verifies both data seeds (build/seed/out); `make test` only runs the seed's offline tests.
seed:
	$(MAKE) -C $(ROOT)/seed build verify BUILD_ROOT=$(BUILD)

# --- static checks ----------------------------------------------------------------------------------
lint:
	$(PY) -I $(ROOT)/tools/lint.py

check-repo:
	$(PY) -I $(ROOT)/tools/check-repo.py

# --- OS image ---------------------------------------------------------------------------------------
os-defconfig:
	$(MAKE) -C $(ROOT)/os defconfig

os-check:
	$(MAKE) -C $(ROOT)/os check-pins check-defconfig test

# --- development servers ----------------------------------------------------------------------------
configurator-dev:
	cd $(ROOT)/configurator && $(PY) -m eitri_configurator --mock --port $(PORT)

clean:
	find $(ROOT) -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf $(ROOT)/.pytest_cache $(ROOT)/*/.pytest_cache $(ROOT)/.ruff_cache $(ROOT)/*/.ruff_cache
