#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract the signing certificate (DER) info from an APK.

Reads the APK Signature Scheme v2/v3 blocks (the scheme FinaSU's repack apksigner
produces) and prints the DER certificate size and its SHA-256 in the exact format
FinaSU's ``kernel/Kbuild`` expects for the second trusted manager slot:

    SIZE_HEX = "0x%04x" % der_size
    SHA256   = lowercase hex digest of the DER certificate

USAGE
-----
    python3 extract_manager_cert.py <apk> [<apk> ...]           # machine format
    python3 extract_manager_cert.py --labeled <apk>             # human format

Default output (one line per unique certificate, stdout)::

    0x02cc 2d89b50aaf1db013c2b6738f973f48b62ea21a3a8d7216bfec3521065d448458

The CI consumes this with ``| head -n1`` and ``awk '{print $1}'`` / ``'{print $2}'``,
so the default format must not change.

With ``--labeled`` it prints instead::

    size=0x02cc
    hash=2d89b50aaf1db013c2b6738f973f48b62ea21a3a8d7216bfec3521065d448458

WHY THIS EXISTS
---------------
The kernel bakes the manager APK's signer certificate into a trust slot
(``KSU_EXPECTED_SIZE2`` / ``KSU_EXPECTED_HASH2`` for FinaSU's second slot).  The
user must install exactly the APK whose certificate is compiled in, otherwise the
manager reports "not installed" / "kernel needs updating" - which looks like a
flashing failure but is really a certificate mismatch.

The format is byte-exact on purpose: ``0x%04x`` of the DER length, then the SHA-256
of the DER blob.  ``build.sh`` upstream derives the same two values from
``manager/keystore.properties``, so they can be cross-checked against each other.

IMPLEMENTATION NOTE
-------------------
The APK Signing Block framing uses **uint64** length prefixes while everything
inside the v2/v3 blocks uses **uint32**.  Mixing them up reads straight into the
``digests`` array and yields a bogus size like ``0x0024`` instead of ``0x02cc`` -
that bug shipped once already, so the offsets below are load-bearing.  In
particular the certificate section is reached by skipping the whole digests block
first::

    signer -> signed_data -> [digests_len][digests][certs_len][certs...]

Keep in sync with the inline parser in the reference repo's
``.github/workflows/get-manager.yml``.

Standard library only.
"""

import hashlib
import struct
import sys

V2_BLOCK_ID = 0x7109871A
V3_BLOCK_ID = 0xF05368C0


def u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def extract_certs(data):
    """Yield DER certificates from the APK signing block, if present."""
    magic_off = data.find(b"APK Sig Block 42")
    if magic_off == -1:
        return
    block_end = magic_off + 16
    block_size = u64(data, magic_off - 8)
    block_start = block_end - block_size - 8
    pairs = data[block_start + 8: block_end - 24]
    # Pair framing uses uint64 length prefixes; everything inside the v2/v3 blocks
    # uses uint32.
    off = 0
    while off + 12 <= len(pairs):
        pair_len = u64(pairs, off)
        off += 8
        pair_id = u32(pairs, off)
        payload = pairs[off + 4: off + pair_len]
        if pair_id in (V2_BLOCK_ID, V3_BLOCK_ID):
            q = 4
            signers_end = 4 + u32(payload, 0)
            while q < signers_end:
                signer_len = u32(payload, q)
                q += 4
                signer = payload[q: q + signer_len]
                q += signer_len
                sd_len = u32(signer, 0)
                sd = signer[4: 4 + sd_len]
                r = 0
                digests_len = u32(sd, r)
                r += 4 + digests_len
                certs_len = u32(sd, r)
                r += 4
                cert_section = sd[r: r + certs_len]
                c = 0
                while c < certs_len:
                    cert_len = u32(cert_section, c)
                    c += 4
                    yield cert_section[c: c + cert_len]
                    c += cert_len
        off += pair_len


def main(argv):
    args = list(argv)
    labeled = False
    if "--labeled" in args:
        labeled = True
        args = [a for a in args if a != "--labeled"]
    if not args:
        print("usage: extract_manager_cert.py [--labeled] <apk> [<apk> ...]",
              file=sys.stderr)
        return 2

    seen = set()
    found = False
    for path in args:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            print("cannot read %s: %s" % (path, e), file=sys.stderr)
            continue
        for der in extract_certs(data):
            digest = hashlib.sha256(der).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found = True
            if labeled:
                print("size=0x%04x" % len(der))
                print("hash=%s" % digest)
            else:
                print("0x%04x %s" % (len(der), digest))
    if not found:
        print("no APK Signature Scheme v2/v3 certificate found "
              "(is this a signed APK?)", file=sys.stderr)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
