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


def encode_catalog_number(n):
    """The five characters a TLE uses for catalog number n: plain digits up to 99999,
    Alpha-5 above (100470 -> 'A0470')."""
    n = int(n)
    if n < 100000:
        return f"{n:05d}"
    lead = n // 10000
    if lead - 10 >= len(ALPHA5):
        raise ValueError(f"catalog number {n} is too large for a TLE (Alpha-5 stops at 339999)")
    return f"{ALPHA5[lead - 10]}{n % 10000:04d}"


def _checksum(line):
    return str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68]) % 10)


def _exp_field(v):
    """TLE's 8-character 'sign, 5-digit mantissa, signed exponent' field (BSTAR, second
    derivative): 0.16931e-3 -> ' 16931-3'. Zero is ' 00000-0'."""
    import math
    if v == 0:
        return " 00000-0"
    exp = math.floor(math.log10(abs(v))) + 1
    mant = int(round(abs(v) / 10 ** exp * 1e5))
    if mant >= 100000:                      # rounding carried into the next power of ten
        mant //= 10
        exp += 1
    return f"{'-' if v < 0 else ' '}{mant:05d}{'-' if exp < 0 else '+'}{abs(exp)}"


def omm_to_tle(omm):
    """(name, line1, line2) from one CelesTrak/Space-Track-style OMM JSON object - the only
    form some objects (catalog number 100000 and up) are published in. The mean-motion
    derivative and BSTAR fields in OMM are the same numbers a TLE carries, so this is
    formatting, not conversion. Raises KeyError/ValueError if the record is incomplete."""
    from datetime import datetime
    epoch = datetime.fromisoformat(str(omm["EPOCH"]).replace("Z", ""))
    day = (epoch.timetuple().tm_yday + (epoch.hour * 3600 + epoch.minute * 60 + epoch.second
                                         + epoch.microsecond / 1e6) / 86400.0)
    catalog = encode_catalog_number(omm["NORAD_CAT_ID"])
    year, _, rest = str(omm["OBJECT_ID"]).partition("-")        # "2026-195F" -> 26195F
    designator = f"{year[-2:]}{rest}".ljust(8)
    ndot = float(omm["MEAN_MOTION_DOT"])
    ndot_s = f"{'-' if ndot < 0 else ' '}{abs(ndot):.8f}".replace("0.", ".", 1)
    l1 = (f"1 {catalog}{str(omm.get('CLASSIFICATION_TYPE') or 'U')[0]} {designator} "
          f"{epoch.year % 100:02d}{day:012.8f} {ndot_s} {_exp_field(float(omm['MEAN_MOTION_DDOT']))} "
          f"{_exp_field(float(omm['BSTAR']))} {int(omm.get('EPHEMERIS_TYPE') or 0)} "
          f"{int(omm['ELEMENT_SET_NO']) % 10000:4d}")
    l1 += _checksum(l1)
    l2 = (f"2 {catalog} {float(omm['INCLINATION']):8.4f} {float(omm['RA_OF_ASC_NODE']):8.4f} "
          f"{int(round(float(omm['ECCENTRICITY']) * 1e7)):07d} {float(omm['ARG_OF_PERICENTER']):8.4f} "
          f"{float(omm['MEAN_ANOMALY']):8.4f} {float(omm['MEAN_MOTION']):11.8f}"
          f"{int(omm['REV_AT_EPOCH']) % 100000:5d}")
    l2 += _checksum(l2)
    return str(omm.get("OBJECT_NAME") or "UNKNOWN"), l1, l2


def relabel_tle(tle1, tle2, norad):
    """Both TLE lines with the catalog number replaced by `norad` (checksums redone).
    SatNOGS gives a brand-new satellite a temporary ID (98xxx) while the TLE itself
    still carries the real NORAD number; relabelling lets the configured number
    (whichever the user typed) match the file, with no manual edit."""
    code = encode_catalog_number(norad)
    out = []
    for line in (tle1, tle2):
        body = line[:2] + code + line[7:68]
        out.append(body + _checksum(body))
    return out[0], out[1]
