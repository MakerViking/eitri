#!/usr/bin/env bash
# Build the firmware data dictionaries the klippy batch tests need.
#
# klippy cannot start without the data dictionary of each MCU, and the
# dictionary only exists as a by-product of compiling the MCU firmware. This
# script compiles an STM32F446 build (the dictionary only has to carry the
# STM32 pin names; the CAN-bus transport keeps the USB/UART pins free of
# reservations) from the exact trees under test.
#
#   tests/build-dicts.sh OUT_DIR [KLIPPER_SRC [KALICO_SRC]]
#
# Needs arm-none-eabi-gcc on PATH (for example the Arm GNU Toolchain 13.3.rel1
# tarball from developer.arm.com) and git, make, python3.
# Defaults follow the Eitri workspace layout, see conftest.py.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
workspace="$(cd -- "$here/../../.." && pwd -P)"
out="${1:?usage: build-dicts.sh OUT_DIR [KLIPPER_SRC [KALICO_SRC]]}"
klipper_src="${2:-$workspace/src/upstream-klipper}"
kalico_src="${3:-$workspace/build/kalico}"

command -v arm-none-eabi-gcc >/dev/null || {
    echo "arm-none-eabi-gcc not found on PATH" >&2
    exit 1
}
mkdir -p "$out"
out="$(cd -- "$out" && pwd -P)"

build() {
    local name="$1" src="$2" work
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' RETURN
    git -C "$src" archive HEAD | tar -x -C "$work"
    cat > "$work/.config" <<'CFG'
CONFIG_LOW_LEVEL_OPTIONS=y
CONFIG_MACH_STM32=y
CONFIG_MACH_STM32F4=y
CONFIG_MACH_STM32F446=y
CONFIG_CLOCK_REF_8M=y
CONFIG_STM32_MMENU_CANBUS_PD0_PD1=y
CFG
    make -C "$work" olddefconfig >/dev/null
    make -C "$work" -j"$(nproc)" >/dev/null
    cp "$work/out/klipper.dict" "$out/$name-stm32f446.dict"
    echo "built $out/$name-stm32f446.dict"
}

build klipper "$klipper_src"
build kalico "$kalico_src"
