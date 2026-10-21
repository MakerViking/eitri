################################################################################
#
# eitri-mainsail
#
################################################################################

EITRI_MAINSAIL_VERSION = 2.19.0
EITRI_MAINSAIL_SITE = https://github.com/mainsail-crew/mainsail/releases/download/v$(EITRI_MAINSAIL_VERSION)
EITRI_MAINSAIL_SOURCE = mainsail.zip
EITRI_MAINSAIL_EXTRA_DOWNLOADS = https://raw.githubusercontent.com/mainsail-crew/mainsail/v$(EITRI_MAINSAIL_VERSION)/LICENSE
EITRI_MAINSAIL_LICENSE = GPL-3.0+
EITRI_MAINSAIL_LICENSE_FILES = LICENSE

# The release asset is a zip with its files at the top level, so it is
# extracted into a dedicated directory.
define EITRI_MAINSAIL_EXTRACT_CMDS
	mkdir -p $(@D)/www
	$(UNZIP) -d $(@D)/www $(EITRI_MAINSAIL_DL_DIR)/$(EITRI_MAINSAIL_SOURCE)
	$(INSTALL) -m 0644 $(EITRI_MAINSAIL_DL_DIR)/LICENSE $(@D)/LICENSE
endef

define EITRI_MAINSAIL_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/share/eitri/ui/mainsail
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/share/eitri/ui/mainsail
	cp -a $(@D)/www/. $(TARGET_DIR)/usr/share/eitri/ui/mainsail/
endef

$(eval $(generic-package))
