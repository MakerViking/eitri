################################################################################
#
# eitri-katapult
#
################################################################################

# Katapult has no release since v0.0.1 (2022), so the version is a full commit
# SHA, matching [katapult] in release.lock.
EITRI_KATAPULT_VERSION = 3b973a485388b4ef0f9efef46f78152e302aea8e
EITRI_KATAPULT_SITE = $(call github,Arksine,katapult,$(EITRI_KATAPULT_VERSION))
EITRI_KATAPULT_LICENSE = GPL-3.0+
EITRI_KATAPULT_LICENSE_FILES = LICENSE

define EITRI_KATAPULT_INSTALL_TARGET_CMDS
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/usr/lib/eitri/katapult/scripts
	$(foreach f,$(wildcard $(@D)/scripts/*.py), \
		$(INSTALL) -m 0755 $(f) $(TARGET_DIR)/usr/lib/eitri/katapult/scripts/$(notdir $(f))
	)
	ln -sf ../lib/eitri/katapult/scripts/flashtool.py $(TARGET_DIR)/usr/bin/katapult-flashtool
endef

$(eval $(generic-package))
