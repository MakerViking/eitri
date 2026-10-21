################################################################################
#
# eitri-configurator
#
################################################################################

EITRI_CONFIGURATOR_VERSION = local
EITRI_CONFIGURATOR_SITE = $(BR2_EXTERNAL_EITRI_PATH)/../configurator
EITRI_CONFIGURATOR_SITE_METHOD = local
EITRI_CONFIGURATOR_LICENSE = Apache-2.0
EITRI_CONFIGURATOR_LICENSE_FILES = $(BR2_EXTERNAL_EITRI_PATH)/../LICENSE
EITRI_CONFIGURATOR_DEPENDENCIES = python3 python-aiohttp python-jinja2

# The application derives its web root and the repository root (for
# release.lock) from the location of its package, so the tree is installed as
# /usr/lib/eitri/configurator/{eitri_configurator,web}. post-build.sh puts
# release.lock one level up, mirroring the repository layout. Tests,
# screenshots, tooling and byte-code caches are not part of the runtime.
define EITRI_CONFIGURATOR_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/eitri/configurator
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/eitri/configurator
	rsync -a --exclude='__pycache__' --exclude='*.pyc' \
		$(@D)/eitri_configurator $(TARGET_DIR)/usr/lib/eitri/configurator/
	rsync -a --exclude='node_modules' --exclude='__pycache__' \
		$(@D)/web $(TARGET_DIR)/usr/lib/eitri/configurator/
endef

$(eval $(generic-package))
