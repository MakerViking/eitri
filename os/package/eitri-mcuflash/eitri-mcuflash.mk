################################################################################
#
# eitri-mcuflash
#
################################################################################

EITRI_MCUFLASH_VERSION = local
EITRI_MCUFLASH_SITE = $(BR2_EXTERNAL_EITRI_PATH)/../installer/mcuflash
EITRI_MCUFLASH_SITE_METHOD = local
EITRI_MCUFLASH_LICENSE = Apache-2.0
EITRI_MCUFLASH_LICENSE_FILES = LICENSE

# The licence lives at the repository root, outside this package's source tree;
# Buildroot reads LICENSE_FILES relative to the build directory.
define EITRI_MCUFLASH_COPY_LICENSE
	cp $(BR2_EXTERNAL_EITRI_PATH)/../LICENSE $(@D)/LICENSE
endef
EITRI_MCUFLASH_POST_RSYNC_HOOKS += EITRI_MCUFLASH_COPY_LICENSE
EITRI_MCUFLASH_DEPENDENCIES = python3

# The package is pure Python with one data file (boards.json). It is copied
# rather than built as a wheel so that the image does not depend on the
# project's packaging metadata (README, version files) being complete.
define EITRI_MCUFLASH_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/eitri_mcuflash
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages
	rsync -a --exclude='__pycache__' --exclude='*.pyc' \
		$(@D)/eitri_mcuflash $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/
	$(INSTALL) -D -m 0755 $(EITRI_MCUFLASH_PKGDIR)/eitri-mcuflash \
		$(TARGET_DIR)/usr/bin/eitri-mcuflash
endef

$(eval $(generic-package))
