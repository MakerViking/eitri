# SPDX-License-Identifier: Apache-2.0
# Eitri OS: wrapper around Buildroot.
#
#   make -C os help          list the targets
#   make -C os image         build the disk image and the RAUC bundle
#
# Buildroot itself is fetched into BUILD_ROOT/buildroot at the version pinned in
# release.lock and driven with BR2_EXTERNAL=os/. Nothing is written inside the
# repository: sources, downloads and build output live under BUILD_ROOT, which
# is ../build next to the repository by default.

SHELL := /bin/bash
.DEFAULT_GOAL := help

OS_DIR     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_ROOT  := $(abspath $(OS_DIR)/..)
BUILD_ROOT ?= $(abspath $(REPO_ROOT)/../build)

LOCKGET := python3 -I $(OS_DIR)/scripts/lockget.py
BUILDROOT_VERSION := $(shell $(LOCKGET) os.buildroot.version)
BUILDROOT_COMMIT  := $(shell $(LOCKGET) os.buildroot.commit)
BUILDROOT_URL     ?= https://gitlab.com/buildroot.org/buildroot.git

BR_DIR  ?= $(BUILD_ROOT)/buildroot
OUT     ?= $(BUILD_ROOT)/br-out
DL_DIR  ?= $(BUILD_ROOT)/br-dl
CCACHE_DIR ?= $(BUILD_ROOT)/br-ccache

DEFCONFIG := eitri_plus4_defconfig
IMAGES    := $(OUT)/images

# Buildroot reads these from the environment.
export BR2_DL_DIR := $(DL_DIR)
export BR2_CCACHE_DIR := $(CCACHE_DIR)

BR := $(MAKE) -C $(BR_DIR) BR2_EXTERNAL=$(OS_DIR) O=$(OUT)

# The checks share work directories and Buildroot does its own parallelism.
.NOTPARALLEL:

.PHONY: help buildroot defconfig ddrbin-config image menuconfig savedefconfig \
	sources legal-info check check-defconfig check-fragments check-pins test clean distclean

help:
	@echo "Eitri OS build (Buildroot $(BUILDROOT_VERSION), pinned in release.lock)"
	@echo
	@echo "  buildroot       fetch Buildroot into $(BR_DIR)"
	@echo "  defconfig       configure $(OUT) from configs/$(DEFCONFIG)"
	@echo "  ddrbin-config   configure with Rockchip's DRAM blob instead of the open TPL"
	@echo "  image           build $(IMAGES)/eitri-plus4.img and the signed .raucb bundle"
	@echo "  menuconfig      edit the configuration, then run savedefconfig"
	@echo "  savedefconfig   write the current configuration back to configs/$(DEFCONFIG)"
	@echo "  sources         download every source without building"
	@echo "  legal-info      collect licences and sources for the build"
	@echo "  check           everything below that needs no full build"
	@echo "    check-defconfig  prove every defconfig option takes effect"
	@echo "    check-fragments  prove the kernel and U-Boot fragments survive Kconfig"
	@echo "    check-pins       compare package pins with release.lock"
	@echo "  test            unit tests of the shell tools (pytest)"
	@echo "  clean           remove $(OUT)"
	@echo "  distclean       remove $(OUT), $(BR_DIR) and the download cache"
	@echo
	@echo "Release builds: EITRI_RELEASE=1 EITRI_VERSION=x.y.z EITRI_RAUC_KEYRING=... \\"
	@echo "                EITRI_RAUC_CERT=... EITRI_RAUC_KEY=... make -C os image"

# --- Buildroot checkout ------------------------------------------------------
# A shallow clone of the pinned tag. The commit is compared with release.lock so
# that a moved or re-tagged ref cannot slip in.
$(BR_DIR)/Makefile:
	mkdir -p $(dir $(BR_DIR))
	git clone --quiet --depth 1 --branch $(BUILDROOT_VERSION) $(BUILDROOT_URL) $(BR_DIR)

buildroot: $(BR_DIR)/Makefile
	@have=$$(git -C $(BR_DIR) rev-parse HEAD); \
	if [ "$$have" != "$(BUILDROOT_COMMIT)" ]; then \
		echo "error: $(BR_DIR) is at $$have, release.lock pins $(BUILDROOT_COMMIT)" >&2; exit 1; \
	fi
	@echo "Buildroot $(BUILDROOT_VERSION) ($(BUILDROOT_COMMIT)) in $(BR_DIR)"

# --- Configuration -----------------------------------------------------------
$(OUT)/.config: $(OS_DIR)/configs/$(DEFCONFIG) | buildroot
	$(BR) $(DEFCONFIG)

defconfig: | buildroot
	$(BR) $(DEFCONFIG)

ddrbin-config: defconfig
	cd $(BR_DIR) && KCONFIG_CONFIG=$(OUT)/.config ./support/kconfig/merge_config.sh -m -O $(OUT) \
		$(OUT)/.config $(OS_DIR)/board/eitri-plus4/ddrbin.config
	$(BR) olddefconfig

menuconfig: $(OUT)/.config
	$(BR) menuconfig

savedefconfig: $(OUT)/.config
	$(BR) BR2_DEFCONFIG=$(OS_DIR)/configs/$(DEFCONFIG) savedefconfig

# --- Build ---------------------------------------------------------------
image: $(OUT)/.config
	$(BR)
	@ls -lh $(IMAGES)/eitri-plus4.img $(IMAGES)/eitri-plus4-*.raucb

sources: $(OUT)/.config
	$(BR) source

legal-info: $(OUT)/.config
	$(BR) legal-info

# --- Checks that need no full build -------------------------------------------
check: check-pins check-defconfig check-fragments test

check-defconfig: | buildroot
	$(OS_DIR)/scripts/check-defconfig.sh $(BR_DIR) $(BUILD_ROOT)/br-check

# The fragments are checked against the kernel and U-Boot sources, which are
# downloaded by Buildroot. KERNEL_SRC and UBOOT_SRC point at extracted trees;
# the target is skipped with a notice when they are not set.
check-fragments:
	@if [ -n "$(KERNEL_SRC)" ]; then \
		$(OS_DIR)/scripts/check-kconfig-fragment.sh -a arm64 -w $(BUILD_ROOT)/kconfig-check-linux \
			$(KERNEL_SRC) $(OS_DIR)/board/eitri-plus4/linux.fragment; \
	else echo "check-fragments: KERNEL_SRC not set, skipping the kernel fragment"; fi
	@if [ -n "$(UBOOT_SRC)" ]; then \
		$(OS_DIR)/scripts/check-kconfig-fragment.sh -d roc-cc-rk3328_defconfig -w $(BUILD_ROOT)/kconfig-check-uboot \
			$(UBOOT_SRC) $(OS_DIR)/board/eitri-plus4/uboot.fragment; \
		cat $(OS_DIR)/board/eitri-plus4/uboot.fragment $(OS_DIR)/board/eitri-plus4/uboot-ddrbin.fragment \
			> $(BUILD_ROOT)/uboot-ddrbin-merged.fragment; \
		$(OS_DIR)/scripts/check-kconfig-fragment.sh -d roc-cc-rk3328_defconfig -w $(BUILD_ROOT)/kconfig-check-uboot-ddrbin \
			$(UBOOT_SRC) $(BUILD_ROOT)/uboot-ddrbin-merged.fragment; \
	else echo "check-fragments: UBOOT_SRC not set, skipping the U-Boot fragments"; fi

check-pins:
	python3 -I $(OS_DIR)/scripts/check-pins.py

# The tests that need rauc or genimage use the ones Buildroot built, when there
# is a build: they are the versions that go into the release.
test:
	$(if $(wildcard $(OUT)/host/bin/rauc),EITRI_RAUC=$(OUT)/host/bin/rauc) \
	$(if $(wildcard $(OUT)/host/bin/genimage),EITRI_GENIMAGE=$(OUT)/host/bin/genimage) \
	python3 -m pytest -q $(OS_DIR)/tests

# --- Cleaning ------------------------------------------------------------
clean:
	rm -rf $(OUT)

distclean: clean
	rm -rf $(BR_DIR) $(DL_DIR) $(CCACHE_DIR)
