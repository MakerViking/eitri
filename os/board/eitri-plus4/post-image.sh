#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Buildroot post-image hook, first of three (post-image.sh, genimage.sh,
# post-bundle.sh). Stages the files that genimage.cfg puts on the FAT boot
# partition next to the other images.
set -euo pipefail

BOARD_DIR=${BR2_EXTERNAL_EITRI_PATH:?run through Buildroot with BR2_EXTERNAL=os/}/board/eitri-plus4

install -m 0644 "$BOARD_DIR/boot/eitri-boot.env" "$BOARD_DIR/boot/README.txt" "${BINARIES_DIR:?}/"
