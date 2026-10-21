#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Create a throwaway RAUC signing hierarchy for development images.
#
# The release channel is signed with keys that never touch a developer machine
# or CI log, and whose CA certificate is committed as
# board/eitri-plus4/rauc/keyring.crt. A development build has neither, so it
# generates its own CA, bakes that CA into the image as the RAUC keyring, and
# signs its bundles with a certificate issued by it. Such an image only accepts
# bundles from the same build tree.
#
# Usage: gen-dev-pki.sh OUTPUT_DIR
# Writes ca.cert.pem, dev.cert.pem and dev.key.pem (mode 0600) into OUTPUT_DIR.
set -euo pipefail

out=${1:?usage: gen-dev-pki.sh OUTPUT_DIR}
mkdir -p "$out"
cd "$out"

if [ -s ca.cert.pem ] && [ -s dev.cert.pem ] && [ -s dev.key.pem ]; then
	exit 0
fi

umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
	-keyout ca.key.pem -out ca.cert.pem -days 3650 \
	-subj "/O=Eitri/CN=Eitri development CA" \
	-addext "basicConstraints=critical,CA:TRUE" \
	-addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
	-keyout dev.key.pem -out dev.csr.pem \
	-subj "/O=Eitri/CN=Eitri development signer" 2>/dev/null

printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n' >ext.cnf
openssl x509 -req -in dev.csr.pem -CA ca.cert.pem -CAkey ca.key.pem -CAcreateserial \
	-out dev.cert.pem -days 825 -extfile ext.cnf 2>/dev/null

rm -f dev.csr.pem ext.cnf ca.srl ca.cert.srl
# The CA private key is not needed once the signer certificate exists.
rm -f ca.key.pem
echo "development PKI written to $out" >&2
