#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: ISS_SSTV
# Author: N6RFM
# GNU Radio version: 3.10.9.2

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
import ISS_SSTV_rig_freq_poller_0 as rig_freq_poller_0  # embedded python block
import filerepeater
import gpredict
import osmosdr
import time



class ISS_SSTV(gr.top_block, Qt.QWidget):

    def __init__(self, freq=437.550e6, gpredict_port=4531, nfreq=437.550e6, offset=50e3, record_iq=1):
        gr.top_block.__init__(self, "ISS_SSTV", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("ISS_SSTV")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "ISS_SSTV")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Parameters
        ##################################################
        self.freq = freq
        self.gpredict_port = gpredict_port
        self.nfreq = nfreq
        self.offset = offset
        self.record_iq = record_iq

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 2500000
        self.BFO = BFO = 0

        ##################################################
        # Blocks
        ##################################################

        self._BFO_range = qtgui.Range(-15000, 15000, 100, 0, 200)
        self._BFO_win = qtgui.RangeWidget(self._BFO_range, self.set_BFO, "'BFO'", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._BFO_win)
        self.rig_freq_poller_0 = rig_freq_poller_0.blk(rig_host='127.0.0.1', rig_port=4532, poll_interval_s=0.5)
        self.osmosdr_source_0 = osmosdr.source(
            args="numchan=" + str(1) + " " + 'driver=airspy,serial=466c64c8323184c7,soapy=0'
        )
        self.osmosdr_source_0.set_time_unknown_pps(osmosdr.time_spec_t())
        self.osmosdr_source_0.set_sample_rate(samp_rate)
        self.osmosdr_source_0.set_biast(False)
        self.osmosdr_source_0.set_center_freq((nfreq-offset), 0)
        self.osmosdr_source_0.set_freq_corr(0, 0)
        self.osmosdr_source_0.set_dc_offset_mode(0, 0)
        self.osmosdr_source_0.set_iq_balance_mode(0, 0)
        self.osmosdr_source_0.set_gain_mode(False, 0)
        self.osmosdr_source_0.set_gain(21, 0)
        self.osmosdr_source_0.set_if_gain(21, 0)
        self.osmosdr_source_0.set_bb_gain(21, 0)
        self.osmosdr_source_0.set_antenna('', 0)
        self.osmosdr_source_0.set_bandwidth(0, 0)
        self.low_pass_filter_0 = filter.fir_filter_ccf(
            50,
            firdes.low_pass(
                1,
                samp_rate,
                50000,
                600,
                window.WIN_HAMMING,
                6.76))
        self.gpredict_MsgPairToVar_0 = gpredict.MsgPairToVar(self.set_freq)
        self.filerepeater_AdvFileSink_0 = filerepeater.AdvFileSink(1, gr.sizeof_gr_complex*1, '/home/bob/Desktop/IQ_Files/', 'ISS_SSTV', freq, 50000, 0, 0,bool(record_iq),False,False, 8,False,False)
        self.blocks_multiply_x_0 = blocks.multiply_vcc(1)
        self.analog_sig_source_x_0 = analog.sig_source_c(samp_rate, analog.GR_COS_WAVE, (-(freq-nfreq+offset)+BFO), 1, 0, 0)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.rig_freq_poller_0, 'freq'), (self.gpredict_MsgPairToVar_0, 'inpair'))
        self.connect((self.analog_sig_source_x_0, 0), (self.blocks_multiply_x_0, 1))
        self.connect((self.blocks_multiply_x_0, 0), (self.low_pass_filter_0, 0))
        self.connect((self.low_pass_filter_0, 0), (self.filerepeater_AdvFileSink_0, 0))
        self.connect((self.osmosdr_source_0, 0), (self.blocks_multiply_x_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "ISS_SSTV")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_freq(self):
        return self.freq

    def set_freq(self, freq):
        self.freq = freq
        self.analog_sig_source_x_0.set_frequency((-(self.freq-self.nfreq+self.offset)+self.BFO))
        self.filerepeater_AdvFileSink_0.setCenterFrequency(self.freq)

    def get_gpredict_port(self):
        return self.gpredict_port

    def set_gpredict_port(self, gpredict_port):
        self.gpredict_port = gpredict_port

    def get_nfreq(self):
        return self.nfreq

    def set_nfreq(self, nfreq):
        self.nfreq = nfreq
        self.analog_sig_source_x_0.set_frequency((-(self.freq-self.nfreq+self.offset)+self.BFO))
        self.osmosdr_source_0.set_center_freq((self.nfreq-self.offset), 0)

    def get_offset(self):
        return self.offset

    def set_offset(self, offset):
        self.offset = offset
        self.analog_sig_source_x_0.set_frequency((-(self.freq-self.nfreq+self.offset)+self.BFO))
        self.osmosdr_source_0.set_center_freq((self.nfreq-self.offset), 0)

    def get_record_iq(self):
        return self.record_iq

    def set_record_iq(self, record_iq):
        self.record_iq = record_iq

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.analog_sig_source_x_0.set_sampling_freq(self.samp_rate)
        self.low_pass_filter_0.set_taps(firdes.low_pass(1, self.samp_rate, 50000, 600, window.WIN_HAMMING, 6.76))
        self.osmosdr_source_0.set_sample_rate(self.samp_rate)

    def get_BFO(self):
        return self.BFO

    def set_BFO(self, BFO):
        self.BFO = BFO
        self.analog_sig_source_x_0.set_frequency((-(self.freq-self.nfreq+self.offset)+self.BFO))



def argument_parser():
    parser = ArgumentParser()
    parser.add_argument(
        "-f", "--freq", dest="freq", type=eng_float, default=eng_notation.num_to_str(float(437.550e6)),
        help="Set frequency [default=%(default)r]")
    parser.add_argument(
        "--gpredict-port", dest="gpredict_port", type=intx, default=4531,
        help="Set GPredict port [default=%(default)r]")
    parser.add_argument(
        "--nfreq", dest="nfreq", type=eng_float, default=eng_notation.num_to_str(float(437.550e6)),
        help="Set Nominal Frequency [default=%(default)r]")
    parser.add_argument(
        "--offset", dest="offset", type=eng_float, default=eng_notation.num_to_str(float(50e3)),
        help="Set centre frequency offset [default=%(default)r]")
    parser.add_argument(
        "--record-iq", dest="record_iq", type=intx, default=1,
        help="Set record_iq [default=%(default)r]")
    return parser


def main(top_block_cls=ISS_SSTV, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(freq=options.freq, gpredict_port=options.gpredict_port, nfreq=options.nfreq, offset=options.offset, record_iq=options.record_iq)

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
