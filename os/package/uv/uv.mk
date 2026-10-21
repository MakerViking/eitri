################################################################################
#
# uv
#
################################################################################

UV_VERSION = 0.13.4
UV_SOURCE = uv-aarch64-unknown-linux-gnu.tar.gz
UV_SITE = https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)
UV_EXTRA_DOWNLOADS = \
	https://raw.githubusercontent.com/astral-sh/uv/$(UV_VERSION)/LICENSE-MIT \
	https://raw.githubusercontent.com/astral-sh/uv/$(UV_VERSION)/LICENSE-APACHE
UV_LICENSE = Apache-2.0 or MIT
UV_LICENSE_FILES = LICENSE-APACHE LICENSE-MIT

# The release tarball carries only the two executables; the licence texts are
# separate downloads and are copied next to them for legal-info.
define UV_INSTALL_EXTRA_LICENSES
	$(INSTALL) -m 0644 $(UV_DL_DIR)/LICENSE-MIT $(UV_DL_DIR)/LICENSE-APACHE $(@D)/
endef
UV_POST_EXTRACT_HOOKS += UV_INSTALL_EXTRA_LICENSES

define UV_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/uv $(TARGET_DIR)/usr/bin/uv
	$(INSTALL) -D -m 0755 $(@D)/uvx $(TARGET_DIR)/usr/bin/uvx
endef

$(eval $(generic-package))
