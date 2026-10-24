# SPDX-License-Identifier: Apache-2.0
"""rauc status is read correctly when rauc logs to stderr around its JSON, as it does on the printer."""
import json

import pytest
from eitri_mcuflash.proc import RunResult
from eitri_update.rauc import RaucClient
from eitri_update.mcufw import FlashError

DOC = {"compatible": "eitri-plus4", "booted": "B", "slots": [
    {"rootfs.0": {"bootname": "A", "state": "inactive", "boot_status": "good", "device": "/dev/mmcblk1p5",
                  "slot_status": {"bundle": {"version": "2026.10.23-dev.83622e0"}}}},
    {"rootfs.1": {"bootname": "B", "state": "booted", "boot_status": "good", "device": "/dev/mmcblk1p6"}}]}


def client(output, rc=0):
    return RaucClient(lambda argv, **kw: RunResult(rc, output))


@pytest.mark.parametrize("output", [
    json.dumps(DOC),
    # verbatim shape from the printer, 2026-10-24
    "rauc-Message: 21:13:03.828: Using central status file /data/eitri/state/rauc.status\n"
    "rauc-Message: 21:13:03.828: Using system config file /etc/rauc/system.conf\n" + json.dumps(DOC) + "\n",
    json.dumps(DOC) + "\nrauc-Message: 21:13:03.900: done\n",
], ids=["plain", "messages-before", "messages-after"])
def test_status_tolerates_rauc_log_lines(output):
    st = client(output).status()
    assert st.compatible == "eitri-plus4" and st.booted == "B"
    assert [(s.bootname, s.boot_status) for s in st.slots] == [("A", "good"), ("B", "good")]


@pytest.mark.parametrize("output", ["", "rauc-Message: only noise\n", "{not json", "[1, 2]"])
def test_status_without_a_json_object_is_still_an_error(output):
    with pytest.raises(FlashError) as e:
        client(output).status()
    assert e.value.code == "rauc"
