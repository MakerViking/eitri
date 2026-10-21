#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Print the Eitri OS version string.
#
# EITRI_VERSION wins, which is how CI stamps a release. Otherwise the version
# comes from git: the nearest tag (a leading "v" is dropped) plus the commit
# distance and hash, or "0.0.0-dev+<hash>" before the first tag.
set -euo pipefail

if [ -n "${EITRI_VERSION:-}" ]; then
	printf '%s\n' "${EITRI_VERSION#v}"
	exit 0
fi

root=$(cd "$(dirname "$0")/../.." && pwd)
if desc=$(git -C "$root" describe --tags --always --dirty 2>/dev/null); then
	if [[ "$desc" == *-* || "$desc" == v[0-9]* ]]; then
		printf '%s\n' "${desc#v}"
	else
		# No tag: describe printed just a hash.
		printf '0.0.0-dev+%s\n' "$desc"
	fi
else
	printf '0.0.0-dev\n'
fi
