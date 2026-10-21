################################################################################
#
# eitri-klipper-layer
#
################################################################################

EITRI_KLIPPER_LAYER_VERSION = local
EITRI_KLIPPER_LAYER_SITE = $(BR2_EXTERNAL_EITRI_PATH)/../klipper-layer
EITRI_KLIPPER_LAYER_SITE_METHOD = local
# The extras are GPL-3.0-or-later (they are Klipper extras); the config set and scripts are Apache-2.0.
EITRI_KLIPPER_LAYER_LICENSE = GPL-3.0-or-later (extras), Apache-2.0 (config, scripts)
EITRI_KLIPPER_LAYER_LICENSE_FILES = extras/LICENSE

# Laid out as <repo>/klipper-layer, one level below /usr/lib/eitri (where release.lock is),
# so that the configurator finds it the same way in a checkout and in the image. Tests, byte-code
# and the pytest configuration are not part of the runtime.
define EITRI_KLIPPER_LAYER_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/eitri/klipper-layer
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/eitri/klipper-layer
	rsync -a --exclude='__pycache__' --exclude='*.pyc' \
		$(@D)/extras $(@D)/config $(@D)/scripts $(@D)/README.md \
		$(TARGET_DIR)/usr/lib/eitri/klipper-layer/
	chmod 0755 $(TARGET_DIR)/usr/lib/eitri/klipper-layer/scripts/*
endef

$(eval $(generic-package))
