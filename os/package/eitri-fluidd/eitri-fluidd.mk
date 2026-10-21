################################################################################
#
# eitri-fluidd
#
################################################################################

EITRI_FLUIDD_VERSION = 1.37.7
EITRI_FLUIDD_SITE = https://github.com/fluidd-core/fluidd/releases/download/v$(EITRI_FLUIDD_VERSION)
EITRI_FLUIDD_SOURCE = fluidd.zip
EITRI_FLUIDD_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/fluidd-core/fluidd/v$(EITRI_FLUIDD_VERSION)/LICENSE
EITRI_FLUIDD_LICENSE = GPL-3.0+
EITRI_FLUIDD_LICENSE_FILES = LICENSE

# The release asset is a zip with its files at the top level, so it is
# extracted into a dedicated directory.
define EITRI_FLUIDD_EXTRACT_CMDS
	mkdir -p $(@D)/www
	$(UNZIP) -d $(@D)/www $(EITRI_FLUIDD_DL_DIR)/$(EITRI_FLUIDD_SOURCE)
	$(INSTALL) -m 0644 $(EITRI_FLUIDD_DL_DIR)/LICENSE $(@D)/LICENSE
endef

define EITRI_FLUIDD_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/share/eitri/ui/fluidd
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/share/eitri/ui/fluidd
	cp -a $(@D)/www/. $(TARGET_DIR)/usr/share/eitri/ui/fluidd/
endef

$(eval $(generic-package))
