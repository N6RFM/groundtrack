#!/usr/bin/env python3
"""
Catalog number of a TLE, read from its line 1 - including numbers of 100000 and
up, which a two-line element set can't spell in five digits. Those use the
"Alpha-5" scheme: the first character is a letter standing for 10-33 (A=10,
B=11 ... H=17, J=18 ... N=22, P=23 ... Z=33; I and O are skipped), so A0470 is
100470 and B1234 is 111234.

Plain int(line1[2:7]) raises on those, which made scripts silently treat the
satellite as "missing" from a TLE file that does contain it. Skyfield/sgp4
(2.23+) decode the same format, so planning itself was never affected - only
this bookkeeping.
"""

ALPHA5 = "ABCDEFGHJKLMNPQRSTUVWXYZ"          # value 10 + index; no I, no O


def catalog_number(line1):
    """The NORAD catalog number on a TLE line 1, or None if it isn't readable."""
    field = line1[2:7].strip() if isinstance(line1, str) else ""
    if len(field) != 5:
        return None
    try:
        if field[0].isalpha():
            lead = ALPHA5.index(field[0].upper())
            return (10 + lead) * 10000 + int(field[1:])
        return int(field)
    except ValueError:
        return None
