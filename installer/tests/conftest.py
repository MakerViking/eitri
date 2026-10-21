import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.abspath(os.path.join(HERE, ".."))
sys.path[:0] = [HERE, os.path.join(INSTALLER, "eitri-update"), os.path.join(INSTALLER, "mcuflash"),
                os.path.join(INSTALLER, "eitri-update", "tests")]
