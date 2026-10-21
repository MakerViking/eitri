################################################################################
#
# go2rtc
#
################################################################################

GO2RTC_VERSION = 1.9.14
GO2RTC_SITE = https://github.com/AlexxIT/go2rtc/releases/download/v$(GO2RTC_VERSION)
GO2RTC_SOURCE = go2rtc_linux_arm64
GO2RTC_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/AlexxIT/go2rtc/v$(GO2RTC_VERSION)/LICENSE
GO2RTC_LICENSE = MIT
GO2RTC_LICENSE_FILES = LICENSE

# The release asset is the bare executable, not an archive.
define GO2RTC_EXTRACT_CMDS
	$(INSTALL) -D -m 0755 $(GO2RTC_DL_DIR)/$(GO2RTC_SOURCE) $(@D)/go2rtc
	$(INSTALL) -D -m 0644 $(GO2RTC_DL_DIR)/LICENSE $(@D)/LICENSE
endef

define GO2RTC_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/go2rtc $(TARGET_DIR)/usr/bin/go2rtc
endef

$(eval $(generic-package))
