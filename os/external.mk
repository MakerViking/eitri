# Eitri BR2_EXTERNAL: package makefiles.
include $(sort $(wildcard $(BR2_EXTERNAL_EITRI_PATH)/package/*/*.mk))

# Firmware for the optional Realtek RTL8710BU (0bda:b711) USB Wi-Fi dongle.
# Buildroot's linux-firmware package does not list rtlwifi/rtl8710bufw_*.bin,
# but the pinned linux-firmware tarball contains them and rtl8xxxu requests
# them by name. The list is only extended when the package is enabled; the
# defconfig also selects BR2_PACKAGE_LINUX_FIRMWARE_RTL_81XX so that the
# package's install step is generated.
ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE),y)
LINUX_FIRMWARE_FILES += \
	rtlwifi/rtl8710bufw_SMIC.bin \
	rtlwifi/rtl8710bufw_UMC.bin
endif
