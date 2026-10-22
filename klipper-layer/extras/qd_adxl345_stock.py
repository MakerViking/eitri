# ADXL345 accelerometer on MCU firmware built from Klipper v0.12 (QIDI stock)
#
# The X-Plus 4 ships its boards with QIDI's Klipper v0.12 firmware. Its ADXL345
# code predates Klipper's shared "sensor_bulk" messages, so the current host's
# [adxl345] fails at connect with "Command format mismatch: query_adxl345 ...".
# This section drives the same chip with the older commands:
#
#   [qd_adxl345_stock]
#   cs_pin: PA4                    # same options as [adxl345]
#   spi_software_sclk_pin: PA5
#   spi_software_mosi_pin: PA7
#   spi_software_miso_pin: PA6
#
#   [resonance_tester]
#   accel_chip: qd_adxl345_stock
#
# The commands ACCELEROMETER_QUERY, ACCELEROMETER_MEASURE, TEST_RESONANCES and
# SHAPER_CALIBRATE work as with [adxl345]. Sample decoding, register setup and
# timing come unchanged from Klipper's own adxl345 module; only the MCU messages
# differ:
#
#                 v0.12 firmware (this module)          current Klipper
#   start/stop    query_adxl345 oid clock rest_ticks    query_adxl345 oid rest_ticks
#   samples       adxl345_data                          sensor_bulk_data
#   status        adxl345_status ... limit_count        sensor_bulk_status ... possible_overflows
#
# Once a board runs firmware built from current Klipper, use [adxl345] again;
# this section then refuses to load, naming the replacement.
#
# Copyright (C) 2026  The Eitri Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import adxl345, bulk_sensor, bus

STOCK_QUERY = "query_adxl345 oid=%c clock=%u rest_ticks=%u"
STOCK_DATA = "adxl345_data oid=%c sequence=%hu data=%*s"
STOCK_STATUS = ("adxl345_status oid=%c clock=%u query_ticks=%u next_sequence=%hu"
                " buffered=%c fifo=%c limit_count=%hu")


# The v0.12 firmware starts measuring at an explicit MCU clock, which must lie in
# the future (a timer in the past shuts the MCU down); Klipper v0.12 used now + 100 ms.
START_DELAY = 0.100


class _StockStatusQuery:
    """The v0.12 status reply, with its fields renamed to what FixedFreqReader expects.
    The first query after a start waits for the start clock, as Klipper v0.12 did."""

    def __init__(self, query):
        self.query = query
        self.min_clock = 0

    def send(self, data=(), minclock=0, reqclock=0):
        minclock, self.min_clock = max(minclock, self.min_clock), 0
        params = dict(self.query.send(data, minclock=minclock, reqclock=reqclock))
        params["possible_overflows"] = params.get("limit_count", 0)
        return params


class _StockReader(bulk_sensor.FixedFreqReader):
    def setup_query_command(self, msgformat, oid, cq):
        self.oid = oid
        self.query_status_cmd = _StockStatusQuery(self.mcu.lookup_query_command(
            msgformat, STOCK_STATUS, oid=oid, cq=cq))
        self.bulk_queue = bulk_sensor.BulkDataQueue(self.mcu, STOCK_DATA, oid)


class _StockQueryCmd:
    """query_adxl345 as Klipper's adxl345 module sends it ([oid, rest_ticks]),
    translated to the v0.12 form ([oid, clock, rest_ticks]); rest_ticks 0 stops."""

    def __init__(self, cmd, mcu, reader):
        self.cmd, self.mcu, self.reader = cmd, mcu, reader

    def send(self, data, **kw):
        oid, rest_ticks = data
        reactor = self.mcu.get_printer().get_reactor()
        print_time = self.mcu.estimated_print_time(reactor.monotonic()) + START_DELAY
        reqclock = self.mcu.print_time_to_clock(print_time)
        self.reader.query_status_cmd.min_clock = reqclock
        return self.cmd.send([oid, reqclock, rest_ticks], reqclock=reqclock)

    def send_wait_ack(self, data, **kw):
        return self.cmd.send_wait_ack([data[0], 0, 0], **kw)


class QdADXL345Stock(adxl345.ADXL345):
    # Same set-up as adxl345.ADXL345.__init__, with the v0.12 command strings.
    def __init__(self, config):
        self.printer = config.get_printer()
        adxl345.AccelCommandHelper(config, self)
        self.axes_map = adxl345.read_axes_map(
            config, adxl345.SCALE_XY, adxl345.SCALE_XY, adxl345.SCALE_Z)
        self.data_rate = config.getint("rate", 3200)
        if self.data_rate not in adxl345.QUERY_RATES:
            raise config.error("Invalid rate parameter: %d" % (self.data_rate,))
        self.spi = bus.MCU_SPI_from_config(config, 3, default_speed=5000000)
        self.mcu = mcu = self.spi.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_adxl345_cmd = None
        mcu.add_config_cmd("config_adxl345 oid=%d spi_oid=%d"
                           % (oid, self.spi.get_oid()))
        mcu.add_config_cmd("query_adxl345 oid=%d clock=0 rest_ticks=0"
                           % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        chip_smooth = self.data_rate * adxl345.BATCH_UPDATES * 2
        self.ffreader = _StockReader(mcu, chip_smooth, "BBBBB")
        self.last_error_count = 0
        self.batch_bulk = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch,
            self._start_measurements, self._finish_measurements,
            adxl345.BATCH_UPDATES)
        self.name = config.get_name().split()[-1]
        hdr = ("time", "x_acceleration", "y_acceleration", "z_acceleration")
        self.batch_bulk.add_mux_endpoint("adxl345/dump_adxl345", "sensor",
                                         self.name, {"header": hdr})

    def _build_config(self):
        if self.mcu.try_lookup_command(STOCK_QUERY) is None:
            raise self.printer.config_error(
                "qd_adxl345_stock: MCU '%s' does not run Klipper v0.12 firmware "
                "(no '%s'); use [adxl345] for this board"
                % (self.mcu.get_name(), STOCK_QUERY))
        cmdqueue = self.spi.get_command_queue()
        self.ffreader.setup_query_command("query_adxl345_status oid=%c",
                                          oid=self.oid, cq=cmdqueue)
        self.query_adxl345_cmd = _StockQueryCmd(
            self.mcu.lookup_command(STOCK_QUERY, cq=cmdqueue), self.mcu, self.ffreader)


def load_config(config):
    return QdADXL345Stock(config)


def load_config_prefix(config):
    return QdADXL345Stock(config)
