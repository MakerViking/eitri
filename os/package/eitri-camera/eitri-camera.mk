################################################################################
#
# eitri-camera
#
################################################################################

EITRI_CAMERA_VERSION = local
EITRI_CAMERA_SITE = $(BR2_EXTERNAL_EITRI_PATH)/../camera
EITRI_CAMERA_SITE_METHOD = local
EITRI_CAMERA_LICENSE = Apache-2.0
EITRI_CAMERA_LICENSE_FILES = $(BR2_EXTERNAL_EITRI_PATH)/../LICENSE

# Only runtime files are installed. bench/ and tests/ stay in the source tree.
define EITRI_CAMERA_INSTALL_TARGET_CMDS
	$(foreach f,$(wildcard $(@D)/systemd/*.service), \
		$(INSTALL) -D -m 0644 $(f) $(TARGET_DIR)/usr/lib/systemd/system/$(notdir $(f))
	)
	$(foreach f,$(wildcard $(@D)/udev/*.rules), \
		$(INSTALL) -D -m 0644 $(f) $(TARGET_DIR)/usr/lib/udev/rules.d/$(notdir $(f))
	)
	$(foreach f,$(wildcard $(@D)/bin/*), \
		$(INSTALL) -D -m 0755 $(f) $(TARGET_DIR)/usr/lib/eitri/camera/$(notdir $(f))
	)
	$(INSTALL) -D -m 0644 $(@D)/moonraker/nginx-webcam.inc \
		$(TARGET_DIR)/etc/nginx/eitri/nginx-webcam.inc
	$(INSTALL) -D -m 0644 $(@D)/moonraker/webcam.cfg \
		$(TARGET_DIR)/usr/share/eitri/factory/camera/webcam.cfg
	$(INSTALL) -D -m 0644 $(@D)/config/camera.env.example \
		$(TARGET_DIR)/usr/share/eitri/factory/camera/camera.env.example
	$(INSTALL) -D -m 0644 $(@D)/go2rtc/go2rtc.yaml \
		$(TARGET_DIR)/usr/share/eitri/factory/camera/go2rtc.yaml
	$(foreach f,$(wildcard $(@D)/go2rtc/profiles/*), \
		$(INSTALL) -D -m 0644 $(f) $(TARGET_DIR)/usr/share/eitri/factory/camera/profiles/$(notdir $(f))
	)
	$(if $(wildcard $(@D)/README.md), \
		$(INSTALL) -D -m 0644 $(@D)/README.md $(TARGET_DIR)/usr/share/doc/eitri/camera/README.md)
endef

$(eval $(generic-package))
