################################################################################
#
# eitri-ustreamer
#
################################################################################

EITRI_USTREAMER_VERSION = 6.67
EITRI_USTREAMER_SITE = $(call github,pikvm,ustreamer,v$(EITRI_USTREAMER_VERSION))
EITRI_USTREAMER_LICENSE = GPL-3.0+
EITRI_USTREAMER_LICENSE_FILES = LICENSE
EITRI_USTREAMER_DEPENDENCIES = jpeg libevent libbsd host-pkgconf

# MJPEG passthrough needs none of the optional back ends (GPIO, DRM, Janus,
# Python bindings, sd_notify), so they stay off. -latomic is added by upstream.
EITRI_USTREAMER_MAKE_OPTS = \
	$(TARGET_CONFIGURE_OPTS) \
	WITH_PTHREAD_NP=1 \
	WITH_SETPROCTITLE=1 \
	WITH_PDEATHSIG=1 \
	WITH_GPIO=0 \
	WITH_SYSTEMD=0 \
	WITH_V4P=0 \
	WITH_PYTHON=0 \
	WITH_JANUS=0

define EITRI_USTREAMER_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) $(EITRI_USTREAMER_MAKE_OPTS) -C $(@D) apps
endef

define EITRI_USTREAMER_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/ustreamer $(TARGET_DIR)/usr/bin/ustreamer
	$(INSTALL) -D -m 0755 $(@D)/ustreamer-dump $(TARGET_DIR)/usr/bin/ustreamer-dump
endef

$(eval $(generic-package))
