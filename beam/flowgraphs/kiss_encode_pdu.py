"""
embedded python block: kiss_encode_pdu

Wraps each incoming PDU in real KISS framing (FEND + command byte +
escaped payload + FEND) before handing it to network_socket_pdu.

network_socket_pdu is a generic GNU Radio core block with no knowledge
of KISS - it just writes whatever PDU bytes it's given straight to the
socket. satellites_kiss_file_sink applies KISS framing internally when
writing to a file, which is why every .kss file this station has ever
produced parses correctly - but nothing was applying that same framing
on the live TCP path, so downstream every PDU arrived as an unframed
blob with no 0xC0 delimiter for SatsDecoder's kiss_read_stream() to
find. Multiple PDUs arriving close together (e.g. during an image
burst) then get coalesced by TCP into one recv() with no way to tell
where one frame ends and the next begins.

Insert this block between satellites_satellite_decoder_0's "out" port
and network_socket_pdu_0's "pdus" input (remove the direct connection,
route through this block instead). No other wiring changes needed.
"""

import numpy as np
from gnuradio import gr
import pmt

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FESC:
            out.append(FESC)
            out.append(TFESC)
        elif b == FEND:
            out.append(FESC)
            out.append(TFEND)
        else:
            out.append(b)
    return bytes(out)


class blk(gr.sync_block):
    """Wrap each PDU in KISS framing (FEND + cmd byte + escaped data + FEND)"""

    def __init__(self, kiss_command=0x00):
        gr.sync_block.__init__(
            self,
            name='kiss_encode_pdu',
            in_sig=None,
            out_sig=None,
        )
        self.kiss_command = kiss_command
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self.handle_msg)
        self.message_port_register_out(pmt.intern('out'))

    def handle_msg(self, msg):
        try:
            if pmt.is_pair(msg):
                data_pmt = pmt.cdr(msg)
            else:
                data_pmt = msg
            payload = bytes(bytearray(pmt.u8vector_elements(data_pmt)))
        except Exception as e:
            print(f"[kiss_encode_pdu] could not extract PDU bytes: {e}")
            return

        framed = bytes([FEND, self.kiss_command]) + kiss_escape(payload) + bytes([FEND])
        out_vec = pmt.init_u8vector(len(framed), list(framed))
        self.message_port_pub(pmt.intern('out'), pmt.cons(pmt.PMT_NIL, out_vec))
