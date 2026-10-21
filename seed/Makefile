# SPDX-License-Identifier: Apache-2.0
# Eitri data seed: build, verify and test. Nothing here touches a printer.
#
#   make -C seed seed      the test suite hook for the root Makefile: runs without building anything and skips
#                          the checks that need built seeds / the Buildroot image / qemu-aarch64
#   make -C seed build     build both seeds into $(OUT) (needs network, uv, zstd and the Buildroot cross toolchain)
#   make -C seed verify    verify every built seed and run its venvs under qemu-aarch64
#   make -C seed repro     build twice from scratch and compare the archive digests
#   make -C seed lock      regenerate the hash-pinned requirement locks (review the diff!)
#
# Variables: VERSION (default 0.0.0.dev0), FLAVOR (klipper|kalico|all), OUT, EITRI_* as in build-seed.sh.

SHELL := /bin/bash
.DEFAULT_GOAL := help

HERE       := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
ROOT       := $(abspath $(HERE)/..)
BUILD_ROOT ?= $(abspath $(ROOT)/../build)
OUT        ?= $(BUILD_ROOT)/seed/out
VERSION    ?= 0.0.0.dev0
FLAVOR     ?= all
PY         ?= python3

.PHONY: help seed test build verify repro lock clean-work

help:
	@sed -n '3,12p' $(lastword $(MAKEFILE_LIST)) | sed 's/^# \{0,1\}//'

# The hook: fast, offline, no builds.
seed: test

test:
	cd $(HERE) && $(PY) -m pytest tests -q -p no:cacheprovider

build:
	EITRI_BUILD_ROOT=$(BUILD_ROOT) $(HERE)/build-seed.sh --flavor $(FLAVOR) --version $(VERSION) --out $(OUT)

verify:
	@set -e; shopt -s nullglob; found=0; \
	for m in $(OUT)/data-seed-*.manifest.json; do found=1; $(HERE)/verify-seed.sh "$$m"; done; \
	[ $$found = 1 ] || { echo "no seeds in $(OUT): run 'make -C seed build'"; exit 1; }

repro:
	@set -e; for i in 1 2; do rm -rf $(BUILD_ROOT)/seed/repro$$i; \
	  EITRI_BUILD_ROOT=$(BUILD_ROOT) $(HERE)/build-seed.sh --flavor $(FLAVOR) --version $(VERSION) \
	    --out $(BUILD_ROOT)/seed/repro$$i --work $(BUILD_ROOT)/seed/repro-work$$i >/dev/null; done; \
	  a=$$(cd $(BUILD_ROOT)/seed/repro1 && sha256sum *.tar.zst); b=$$(cd $(BUILD_ROOT)/seed/repro2 && sha256sum *.tar.zst); \
	  echo "$$a"; echo "$$b"; [ "$$a" = "$$b" ] && echo "reproducible: identical archives" || { echo "NOT reproducible"; exit 1; }

lock:
	UV=$${UV:-uv} $(HERE)/tools/lock.sh

clean-work:
	rm -rf $(BUILD_ROOT)/seed/work $(BUILD_ROOT)/seed/repro1 $(BUILD_ROOT)/seed/repro2 $(BUILD_ROOT)/seed/repro-work1 $(BUILD_ROOT)/seed/repro-work2
