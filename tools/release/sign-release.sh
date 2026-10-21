#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Sign an Eitri release: the only step of a release that touches a private key.
#
#   tools/release/sign-release.sh --in UNSIGNED_DIR --out SIGNED_DIR --version 2026.10.0 \
#       --base-url https://github.com/OWNER/REPO/releases/download/v2026.10.0/ --generated 2026-10-22T09:00:00Z
#
# It runs in two places, with the same code:
#   * CI: the "sign" job of .github/workflows/release.yml, inside the protected "release" environment. The keys are
#     written to a private temporary directory from the environment's secrets and removed afterwards.
#   * Offline: on Thomas's machine, with the offline keys, on a downloaded "release-unsigned" artifact. That is the
#     fallback when CI signing is unavailable or must not be trusted (docs/RELEASING.md).
#
# Inputs (UNSIGNED_DIR): the build outputs plus INPUTS.sha256, the digests the build recorded. Nothing is read from
# anywhere else; nothing is downloaded.
#
# Steps, in order, and the script stops at the first failure:
#   1. preflight (inputs): digests, version, seeds against release.lock, trust roots committed, no key material
#   2. re-sign every RAUC bundle with the release signing certificate (rauc resign), and check the result against the
#      RAUC keyring that is baked into the image
#   3. build install.json / manifest.json / release-plan.json from the files now in SIGNED_DIR
#   4. preflight (metadata): the documents agree with the files, release.lock and the printer's own parsers
#   5. minisign install.json and manifest.json, then SHA256SUMS over everything (and sign that)
#   6. verify every signature with the code the printer runs (eitri_update.minisign)
#
# Keys (files; paths in the environment, never on the command line, never copied):
#   EITRI_MINISIGN_KEY_FILE   minisign secret key
#   EITRI_MINISIGN_PASSWORD   its password; unset = minisign asks on the terminal (an empty value is an unencrypted key)
#   EITRI_RAUC_KEY_FILE       PEM private key of the RAUC code-signing certificate
#   EITRI_RAUC_CERT_FILE      its certificate (PEM), issued by the CA in the keyring
# Other settings (all optional):
#   EITRI_RAUC_KEYRING        the CA certificate the images trust (default: os/board/eitri-plus4/rauc/keyring.crt)
#   EITRI_KEYS_DIR            trusted minisign public keys (default: installer/keys)
#   EITRI_RAUC_CONF           RAUC system.conf used to verify (default: the image's)
#   EITRI_RAUC, EITRI_MINISIGN, EITRI_PYTHON   tool paths
set -euo pipefail
umask 077

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${EITRI_PYTHON:-python3}
RAUC=${EITRI_RAUC:-rauc}
MINISIGN=${EITRI_MINISIGN:-minisign}
RELEASE_PY=("$PY" -I "$HERE/release.py" --root "$ROOT")

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
die() { echo "sign-release: $*" >&2; exit 1; }

in_dir="" out_dir="" version="" base_url="" generated="" previous="" tag="" lock=""
while [ $# -gt 0 ]; do
	case $1 in
		--in) in_dir=${2:?}; shift 2 ;;
		--out) out_dir=${2:?}; shift 2 ;;
		--version) version=${2:?}; shift 2 ;;
		--base-url) base_url=${2:?}; shift 2 ;;
		--generated) generated=${2:?}; shift 2 ;;
		--previous) previous=${2:?}; shift 2 ;;
		--tag) tag=${2:?}; shift 2 ;;
		--lock) lock=${2:?}; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) die "unknown argument: $1 (see --help)" ;;
	esac
done
[ -n "$in_dir" ] && [ -n "$out_dir" ] && [ -n "$version" ] && [ -n "$base_url" ] || die "--in, --out, --version and --base-url are required"
[ -d "$in_dir" ] || die "$in_dir is not a directory"
[ ! -e "$out_dir" ] || die "$out_dir exists: refusing to mix a new signing run with old files"
tag=${tag:-v$version}
[ -z "$lock" ] || RELEASE_PY+=(--lock "$lock")
keys_dir=${EITRI_KEYS_DIR:-$ROOT/installer/keys}
keyring=${EITRI_RAUC_KEYRING:-$ROOT/os/board/eitri-plus4/rauc/keyring.crt}
image_rauc_conf=${EITRI_RAUC_CONF:-$ROOT/os/board/eitri-plus4/rootfs-overlay/etc/rauc/system.conf}

# --- keys: required, readable, and never inside the directories we are about to publish ---------------------------
minisign_key=${EITRI_MINISIGN_KEY_FILE:-}
rauc_key=${EITRI_RAUC_KEY_FILE:-}
rauc_cert=${EITRI_RAUC_CERT_FILE:-}
[ -n "$minisign_key" ] && [ -f "$minisign_key" ] || die "EITRI_MINISIGN_KEY_FILE must name the minisign secret key"
real_in=$(cd "$in_dir" && pwd -P)
for k in "$minisign_key" "$rauc_key" "$rauc_cert"; do
	[ -z "$k" ] && continue
	case $(cd "$(dirname "$k")" && pwd -P)/ in "$real_in"/*) die "a key file lives inside $in_dir; keys must never be in the artifact directory" ;; esac
done

mapfile -t bundles < <(cd "$in_dir" && find . -maxdepth 1 -type f -name '*.raucb' -printf '%f\n' | sort)
if [ "${#bundles[@]}" -gt 0 ]; then
	[ -n "$rauc_key" ] && [ -f "$rauc_key" ] || die "EITRI_RAUC_KEY_FILE must name the RAUC signing key (the release has RAUC bundles)"
	[ -n "$rauc_cert" ] && [ -f "$rauc_cert" ] || die "EITRI_RAUC_CERT_FILE must name the RAUC signing certificate"
	[ -f "$keyring" ] || die "RAUC keyring $keyring not found: the CA certificate has to be committed before a release"
	command -v "$RAUC" >/dev/null || die "rauc is not installed (EITRI_RAUC)"
fi
command -v "$MINISIGN" >/dev/null || die "minisign is not installed (EITRI_MINISIGN)"

# --- 1. the inputs ---------------------------------------------------------------------------------------------------
echo "== 1/6 preflight: inputs"
"${RELEASE_PY[@]}" preflight --stage inputs --dir "$in_dir" --tag "$tag" --keys-dir "$keys_dir" --keyring "$keyring"

mkdir -p "$out_dir"
trap 'rc=$?; if [ $rc -ne 0 ]; then rm -rf "$out_dir"; echo "sign-release: FAILED, $out_dir removed" >&2; fi' EXIT
for f in "$in_dir"/*; do
	[ -f "$f" ] || continue
	[ "$(basename "$f")" = INPUTS.sha256 ] && continue
	cp -- "$f" "$out_dir/"
done

# --- 2. RAUC bundles -------------------------------------------------------------------------------------------------
echo "== 2/6 RAUC: re-sign ${#bundles[@]} bundle(s)"
# The image's own RAUC settings decide how a signature is judged (code-signing purpose, signing time instead of the
# clock). Take exactly those, minus everything about the board: no bootloader, no slots, the committed keyring.
rauc_tmp=$(mktemp -d)
trap 'rc=$?; rm -rf "$rauc_tmp"; if [ $rc -ne 0 ]; then rm -rf "$out_dir"; echo "sign-release: FAILED, $out_dir removed" >&2; fi' EXIT
rauc_conf=$rauc_tmp/system.conf
if [ "${#bundles[@]}" -gt 0 ]; then
	{
		echo "[system]"; echo "bootloader=noop"; echo "statusfile=$rauc_tmp/status"
		awk '/^\[/ { sec = $0; next } sec == "[system]" && /^(compatible|bundle-formats)=/ { print }' "$image_rauc_conf"
		echo; echo "[keyring]"; echo "path=$keyring"
		awk '/^\[/ { sec = $0; next } sec == "[keyring]" && !/^path=/ && !/^#/ && NF { print }' "$image_rauc_conf"
	} > "$rauc_conf"
	grep -q '^compatible=' "$rauc_conf" || die "$image_rauc_conf has no compatible= in [system]"
fi
for b in "${bundles[@]}"; do
	# --no-verify: the build signed the bundle with a throwaway key. Its integrity comes from INPUTS.sha256, which
	# preflight just checked, not from that signature.
	"$RAUC" --conf="$rauc_conf" resign --no-verify --signing-keyring="$keyring" --cert="$rauc_cert" --key="$rauc_key" \
		"$in_dir/$b" "$out_dir/$b.resigned"
	mv -f -- "$out_dir/$b.resigned" "$out_dir/$b"
	# What the printer will do: verify the new signature against the image's keyring and configuration.
	"$RAUC" --conf="$rauc_conf" info --keyring="$keyring" "$out_dir/$b" >/dev/null || die "$b does not verify against $keyring after re-signing"
	echo "   $b: signed by the release certificate, verifies against the image keyring"
done

# --- 3-4. metadata ---------------------------------------------------------------------------------------------------
echo "== 3/6 metadata"
meta=("${RELEASE_PY[@]}" metadata --artifacts "$out_dir" --out "$out_dir" --version "$version" --base-url "$base_url" --require manifest)
[ -z "$generated" ] || meta+=(--generated "$generated")
[ -z "$previous" ] || meta+=(--previous "$previous")
"${meta[@]}"
echo "== 4/6 preflight: metadata"
"${RELEASE_PY[@]}" preflight --stage metadata --dir "$out_dir" --tag "$tag" --keys-dir "$keys_dir" --keyring "$keyring" \
	--sums "$in_dir/INPUTS.sha256"

# --- 5. minisign -----------------------------------------------------------------------------------------------------
sign_file() { # FILE
	local f=$1 comment="eitri $version $(basename "$1")"
	if [ "${EITRI_MINISIGN_PASSWORD+set}" = set ]; then
		printf '%s\n' "$EITRI_MINISIGN_PASSWORD" | "$MINISIGN" -S -s "$minisign_key" -m "$f" -t "$comment" >/dev/null
	else
		"$MINISIGN" -S -s "$minisign_key" -m "$f" -t "$comment" >/dev/null
	fi
	[ -s "$f.minisig" ] || die "minisign wrote no signature for $f"
}
echo "== 5/6 minisign"
for f in install.json manifest.json; do
	[ -f "$out_dir/$f" ] || continue
	sign_file "$out_dir/$f"
	echo "   $f signed"
done
(cd "$out_dir" && find . -maxdepth 1 -type f ! -name SHA256SUMS ! -name SHA256SUMS.minisig -printf '%f\n' | LC_ALL=C sort | xargs -d '\n' sha256sum > SHA256SUMS)
sign_file "$out_dir/SHA256SUMS"
echo "   SHA256SUMS signed"

# --- 6. verify as a printer would --------------------------------------------------------------------------------------
echo "== 6/6 verify"
"${RELEASE_PY[@]}" verify-signed --dir "$out_dir" --keys-dir "$keys_dir"
(cd "$out_dir" && sha256sum --check --strict SHA256SUMS >/dev/null) || die "SHA256SUMS does not verify"
for f in "$out_dir"/*; do
	[ -f "$f" ] || continue
	if grep -qaE -e '-----BEGIN [A-Z ]*PRIVATE KEY|minisign encrypted secret key' "$f" 2>/dev/null; then die "key material found in $(basename "$f")"; fi
done
echo "signed release written to $out_dir"
rm -rf "$rauc_tmp"
trap - EXIT
