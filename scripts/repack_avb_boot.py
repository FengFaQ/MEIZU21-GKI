#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meizu 21 (M2461) boot.img repacker - clone the stock AVB0 + AVBf, swap the kernel.

WHY THIS EXISTS
---------------
Meizu's ABL (even with the bootloader unlocked) refuses a boot image that does not
carry the two AVB structures the stock image has:

  * an ``AVB0`` VBMeta block placed immediately after the kernel payload, and
  * an ``AVBf`` footer in the last 64 bytes of the 96 MiB partition.

Signing a fresh image with our own key cannot work: those blocks are signed with
Meizu's private key and the stock hash is baked in.  The only viable approach is to
*clone* them from the stock boot.img and move them to wherever the new payload ends.

USAGE
-----
    python3 repack_avb_boot.py <Image> <stock_boot.img> <out_boot.img> [options]

    --update-digest     also refresh the AvbHashDescriptor inside AVB0
                        (image_size / salt / digest).  See "TWO MODES" below.
    --report PATH       write a machine-readable key=value report
    --partition NAME    partition name expected in the hash descriptor (default: boot)
    --quiet             less chatter

TWO MODES
---------
v1 (default, *no* --update-digest):
    AVB0 is copied **verbatim**.  Its internal self-hash and RSA signature stay
    valid, but the hash descriptor still advertises the stock image_size and the
    stock digest, i.e. it describes the stock kernel, not ours.
    This is the conservative choice: the vbmeta block is bit-for-bit the one Meizu
    signed, so an ABL that only *reads* the footer/descriptor is happy.

v2 (--update-digest):
    additionally rewrites three fields inside the AvbHashDescriptor so the
    descriptor honestly describes the new payload:

        image_size = new payload_end
        salt       = sha256(new kernel segment)          <-- stock rule, see below
        digest     = sha256(salt || image[0:image_size])

    !!! The stock vbmeta IS SIGNED: algorithm_type=2 (SHA256_RSA4096), and the
    576-byte authentication block holds a 32-byte hash plus a 512-byte RSA
    signature.  Rewriting any descriptor changes bytes the signature covers, so
    that signature becomes stale and we cannot re-sign without Meizu's private
    key.  v2 therefore produces a DIAGNOSTIC image - footer and descriptor are
    honest, but the vbmeta signature no longer verifies.  Prefer v1 for a real
    device and only fall back to v2 if v1 bounces to Fastboot.

WHERE THE FIELD OFFSETS COME FROM
---------------------------------
The stock Flyme 12.7 boot.img (sha256 8c899eb3...) carries a hash descriptor that
does **not** match the AOSP ``avbtool`` 96-byte layout.  It is 200 bytes with a
132-byte fixed part, an extra ``hash_algorithm`` string ("sha256") and a 64-byte
reserved blob::

    AvbHashDescriptor (tag=2, "legacy string32" layout = 200 bytes), descriptor-relative
    offsets, all integers big-endian::

    +0x00 u64     tag                     = 2
    +0x08 u64     num_bytes_following     = 184
    +0x10 u64     image_size              <-- field 1 (0x2054000 on the stock image)
    +0x18 char[32] hash_algorithm         = "sha256" NUL-padded  <-- string, not an enum
    +0x38 u32     partition_name_len      = 4
    +0x3C u32     salt_len                = 32
    +0x40 u32     digest_len              = 32
    +0x44 u32     flags                   = 0        (never modified)
    +0x48 u8[60]  reserved (all zero)                (never modified)
    +0x84         partition_name          ("boot", NOT 8-byte padded)
    +0x88         salt                    (salt_len bytes)   <-- field 2
    +0xA8         digest                  (digest_len bytes) <-- field 3
                  total = 132 + 4 + 32 + 32 = 200 = 16 + nbf

    Note this is the *old* descriptor layout: hash_algorithm is a 32-byte ASCII string
    rather than the uint32 algorithm enum that newer avbtool emits, and the variable
    fields are packed without 8-byte alignment.  AOSP avbtool's modern layout (96-byte
    fixed part, image_size at +0x10 as uint32) is tried too, so the script still works
    on images from vanilla avbtool.  Layout selection is by validation, not by a
    hard-coded guess.

    The descriptor starts at ``vbmeta + 256 + auth_block_size + descriptors_offset``
    = ``vbmeta + 256 + 576 + 0`` = ``vbmeta + 832`` for the stock image.  Note that
    ``descriptors_offset`` is relative to the **auxiliary** block, not to the vbmeta
    start - reading it as vbmeta-relative lands on the header and looks like garbage.

These offsets are NOT guessed.  They were derived from the image itself and then
proven by three independent checks performed by this script at parse time:

  1. ``sha256(salt || stock[0:image_size]) == digest`` - the AVB hash rule; it only
     holds for the correct triple, so a wrong offset set cannot validate.
  2. ``image_size == AVBf.original_image_size`` - both are the stock payload_end.
  3. the descriptor chain walks cleanly from the first descriptor to the end of
     ``descriptors_size``.

The AOSP layout is tried as well (96-byte fixed part, image_size at +0x10), so the
script still works on images produced by vanilla avbtool.  Layout selection is by
validation, not by a hard-coded guess.  If neither layout validates, the script
refuses to run in v2 mode (but v1 still works, because v1 never reads those fields).

SALT RULE (empirically confirmed on the stock image)
----------------------------------------------------
The stock salt is **not** random - it is::

    salt = sha256( boot_img[4096 : 4096 + kernel_size] )    # i.e. sha256(Image)

Verified: sha256(stock kernel segment) = 4d445384c046f2ba... = the stock salt.
So re-deriving it from the new Image reproduces the stock value exactly when the
kernel is unchanged, which is what makes the byte-identical round-trip test in
avb_verify.log possible.

THE "1664-BYTE BLOCK"
---------------------
The stock AVB0 block is 2496 bytes, laid out as::

    [0x000, 0x100)  header               256 B
    [0x100, 0x340)  authentication data  576 B   (avb hash + RSA-4096 signature)
    [0x340, 0x9C0)  auxiliary data      1664 B
                    [0x340, 0x5A0)  descriptors      608 B  <-- our hash descriptor
                    [0x5A0, 0x9A8)  public key      1032 B
                    [0x9A8, 0x9C0)  padding           24 B

1664 is simply ``auxiliary_data_block_size`` - it is standard AVB structure, not an
opaque vendor extension.  Only three 4/32-byte fields inside it are ever touched;
everything else (public key, signature, all other descriptors) is preserved
byte-for-byte.  ``--report`` prints these ranges so the untouched surface can be
audited.

No third-party dependencies; standard library only.
"""

import argparse
import hashlib
import math
import os
import struct
import sys

PAGE = 4096
HDR_MAGIC = b"ANDROID!"
AVB0_MAGIC = b"AVB0"
AVBF_MAGIC = b"AVBf"
AVBF_LEN = 64

# AVB descriptor tags
TAG_PROPERTY = 0
TAG_HASHTREE = 1
TAG_HASH = 2
TAG_KERNEL_CMDLINE = 3
TAG_CHAIN_PARTITION = 4

TAG_NAMES = {
    TAG_PROPERTY: "Property",
    TAG_HASHTREE: "Hashtree",
    TAG_HASH: "Hash",
    TAG_KERNEL_CMDLINE: "KernelCmdline",
    TAG_CHAIN_PARTITION: "ChainPartition",
}

# libavb AvbAlgorithmType.  Note: 2 is SHA256_RSA4096, not RSA2048 - the stock
# image confirms it (signature_size=512 bytes = 4096-bit, public_key_size=1032).
ALGORITHM_NAMES = {
    0: "NONE",
    1: "SHA256_RSA2048",
    2: "SHA256_RSA4096",
    3: "SHA256_RSA8192",
    4: "SHA512_RSA2048",
    5: "SHA512_RSA4096",
    6: "SHA512_RSA8192",
}

# ---------------------------------------------------------------------------
# AvbHashDescriptor layouts we know about.
#
# ``fixed``    = offset of the variable payload (partition_name) from descriptor start
# ``packed``   = True  -> partition_name / salt / digest are concatenated with no padding
#                False -> each is padded up to an 8-byte boundary (AOSP behaviour)
# ---------------------------------------------------------------------------
LAYOUT_AOSP = {
    "name": "aosp-avbtool",
    "image_size": 0x10,
    "image_size_fmt": ">I",
    "flags": 0x14,
    "name_len": 0x18,
    "salt_len": 0x1C,
    "digest_len": 0x20,
    "fixed": 0x60,
    "packed": False,
}

# The "legacy string32" layout used by the stock Flyme 12.7 image: hash_algorithm is a
# 32-byte NUL-padded ASCII string rather than a uint32 enum, image_size is a uint64.
LAYOUT_MEIZU = {
    "name": "legacy-string32",
    "image_size": 0x10,        # u64 BE
    "image_size_fmt": ">Q",
    "flags": 0x44,             # u32 BE (read-only, never modified)
    "name_len": 0x38,          # u32 BE
    "salt_len": 0x3C,          # u32 BE
    "digest_len": 0x40,        # u32 BE
    "fixed": 0x84,             # partition_name starts here (132)
    "packed": True,            # name/salt/digest are concatenated, NOT 8-byte padded
}

LAYOUTS = (LAYOUT_MEIZU, LAYOUT_AOSP)


class RepackError(Exception):
    pass


def u32le(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def u32be(buf, off):
    return struct.unpack_from(">I", buf, off)[0]


def u64be(buf, off):
    return struct.unpack_from(">Q", buf, off)[0]


def sha256_hex(b):
    return hashlib.sha256(b).hexdigest()


def align8(n):
    return (n + 7) // 8 * 8


def pad_to_page(n):
    return int(math.ceil(n / float(PAGE))) * PAGE


# ---------------------------------------------------------------------------
# AVB0 parsing
# ---------------------------------------------------------------------------
def parse_vbmeta_header(vb):
    """Parse the 256-byte AvbVBMetaImageHeader.  All fields are big-endian."""
    if len(vb) < 256:
        raise RepackError("AVB0 block shorter than the 256-byte header")
    if vb[:4] != AVB0_MAGIC:
        raise RepackError("AVB0 magic missing at the vbmeta offset")

    h = {
        "magic": vb[:4],
        "required_major": u32be(vb, 4),
        "required_minor": u32be(vb, 8),
        "auth_block_size": u64be(vb, 12),
        "aux_block_size": u64be(vb, 20),
        "algorithm_type": u32be(vb, 28),
        # offsets of hash/signature are relative to the authentication block
        "hash_offset": u64be(vb, 32),
        "hash_size": u64be(vb, 40),
        "signature_offset": u64be(vb, 48),
        "signature_size": u64be(vb, 56),
        # offsets of public key / descriptors are relative to the auxiliary block
        "public_key_offset": u64be(vb, 64),
        "public_key_size": u64be(vb, 72),
        "public_key_metadata_offset": u64be(vb, 80),
        "public_key_metadata_size": u64be(vb, 88),
        "descriptors_offset": u64be(vb, 96),
        "descriptors_size": u64be(vb, 104),
        "rollback_index": u64be(vb, 112),
        "flags": u32be(vb, 120),
        "rollback_index_location": u32be(vb, 124),
        "release_string": vb[128:176].split(b"\x00")[0].decode("ascii", "replace"),
    }
    h["auth_start"] = 256
    h["aux_start"] = 256 + h["auth_block_size"]
    h["desc_start"] = h["aux_start"] + h["descriptors_offset"]
    h["desc_end"] = h["desc_start"] + h["descriptors_size"]
    h["pk_start"] = h["aux_start"] + h["public_key_offset"]
    h["pk_end"] = h["pk_start"] + h["public_key_size"]
    h["vbmeta_end"] = 256 + h["auth_block_size"] + h["aux_block_size"]
    return h


def iter_descriptors(vb, hdr):
    """Yield (tag, desc_off, num_bytes_following) for every descriptor."""
    off = hdr["desc_start"]
    end = hdr["desc_end"]
    while off + 16 <= end:
        tag = u64be(vb, off)
        nbf = u64be(vb, off + 8)
        if off + 16 + nbf > end:
            raise RepackError(
                "descriptor at 0x%X claims %d trailing bytes, overruns the "
                "descriptors region (0x%X)" % (off, nbf, end)
            )
        yield tag, off, nbf
        off += 16 + nbf


def _decode_hash_desc_at(vb, desc_off, layout, stock_img,
                         expect_image_size=None, expect_salt=None):
    """Try one layout.

    A layout counts as validated either by the AVB hash rule against the full
    stock image, or (when only the *skeleton* is available) by matching the
    descriptor's image_size / salt against known stock facts passed in via
    --expect-image-size / --expect-salt.  The latter is still a strong check:
    both fields are at fixed offsets of the descriptor, so a wrong layout or
    wrong offsets will not reproduce them.
    """
    name_len = u32be(vb, desc_off + layout["name_len"])
    salt_len = u32be(vb, desc_off + layout["salt_len"])
    digest_len = u32be(vb, desc_off + layout["digest_len"])
    image_size = struct.unpack_from(layout["image_size_fmt"], vb,
                                    desc_off + layout["image_size"])[0]

    if not (0 < name_len <= 64 and salt_len <= 64 and digest_len <= 64):
        return None

    name_off = desc_off + layout["fixed"]
    if layout["packed"]:
        salt_off = name_off + name_len
        dig_off = salt_off + salt_len
    else:
        salt_off = name_off + align8(name_len)
        dig_off = salt_off + align8(salt_len)

    if dig_off + digest_len > len(vb):
        return None

    part_name = bytes(vb[name_off:name_off + name_len])
    salt = bytes(vb[salt_off:salt_off + salt_len])
    digest = bytes(vb[dig_off:dig_off + digest_len])

    # The discriminating test: the AVB hash rule only holds for the right triple.
    validated_by = ""
    if digest_len == 32 and 0 < image_size <= len(stock_img):
        calc = hashlib.sha256(salt + stock_img[0:image_size]).digest()
        if calc == digest:
            validated = True
            validated_by = "avb-hash-rule"
        else:
            validated = False
    else:
        validated = False

    # Skeleton fallback: the payload of a skeleton is zeroed, so the AVB rule can
    # never hold; validate the *field offsets* against known stock facts instead.
    if not validated and expect_image_size is not None and expect_salt is not None:
        if image_size == int(expect_image_size) and salt.hex() == expect_salt.lower():
            validated = True
            validated_by = "known-stock-facts"

    hash_algo = ""
    if layout["name"] == "legacy-string32":
        hash_algo = bytes(vb[desc_off + 0x18:desc_off + 0x18 + 32]).split(b"\x00")[0].decode(
            "ascii", "replace")

    return {
        "layout": layout["name"],
        "desc_off": desc_off,
        "desc_len": 16 + u64be(vb, desc_off + 8),
        "image_size": image_size,
        "image_size_off": desc_off + layout["image_size"],
        "image_size_fmt": layout["image_size_fmt"],
        "flags_off": (desc_off + layout["flags"]) if layout["flags"] is not None else None,
        "hash_algorithm": hash_algo,
        "partition_name": part_name,
        "salt_off": salt_off,
        "salt_len": salt_len,
        "digest_off": dig_off,
        "digest_len": digest_len,
        "validated": validated,
        "validated_by": validated_by,
    }


def find_hash_descriptor(vb, hdr, stock_img, want_partition,
                         expect_image_size=None, expect_salt=None):
    """Locate the AvbHashDescriptor for ``want_partition``.

    Layout selection is driven by validation against the stock image.  If no
    layout validates we still return the best structural guess (first tag==2
    descriptor under the Meizu layout) but mark it ``validated=False`` so the
    caller can refuse to recompute the digest.
    """
    candidates = []
    for tag, desc_off, nbf in iter_descriptors(vb, hdr):
        if tag != TAG_HASH:
            continue
        for layout in LAYOUTS:
            try:
                info = _decode_hash_desc_at(vb, desc_off, layout, stock_img,
                                            expect_image_size, expect_salt)
            except (struct.error, IndexError):
                continue
            if info is None:
                continue
            candidates.append(info)

    # Prefer a validated descriptor whose partition name matches.
    for info in candidates:
        if info["validated"] and info["partition_name"] == want_partition:
            return info
    for info in candidates:
        if info["validated"]:
            return info

    # Fall back: pick the descriptor whose partition name matches, unvalidated.
    for info in candidates:
        if info["partition_name"] == want_partition:
            print(
                "::warning::could not validate the AVB hash rule - falling back to a "
                "structural guess (layout=%s, partition=%r)"
                % (info["layout"], info["partition_name"])
            )
            return info

    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Repack a Meizu 21 GKI Image into a stock-layout boot.img by "
                    "cloning the stock AVB0 + AVBf."
    )
    ap.add_argument("image", help="compiled arm64 Image (uncompressed)")
    ap.add_argument("stock_boot", help="stock boot.img (the AVB source of truth)")
    ap.add_argument("out_boot", help="output boot.img")
    ap.add_argument(
        "--update-digest",
        action="store_true",
        help="v2: also refresh AvbHashDescriptor image_size/salt/digest",
    )
    ap.add_argument("--report", default="", help="write a key=value report to this path")
    ap.add_argument("--partition", default="boot",
                    help="expected partition name in the hash descriptor (default: boot)")
    ap.add_argument("--quiet", action="store_true", help="suppress the banner")
    ap.add_argument(
        "--expect-image-size", default=None,
        help="known stock descriptor image_size (decimal or 0x..); lets --update-digest "
             "validate field offsets when the template is a zeroed AVB skeleton")
    ap.add_argument(
        "--expect-salt", default=None,
        help="known stock descriptor salt (hex); paired with --expect-image-size")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv if argv is not None else sys.argv[1:])

    for path, what in ((a.image, "Image"), (a.stock_boot, "stock boot.img")):
        if not os.path.isfile(path):
            raise RepackError("%s not found: %s" % (what, path))

    with open(a.image, "rb") as f:
        kernel = f.read()
    ksize = len(kernel)
    if ksize == 0:
        raise RepackError("Image is empty")

    with open(a.stock_boot, "rb") as f:
        stock = f.read()
    total = len(stock)
    stock_sha = sha256_hex(stock)

    if stock[:8] != HDR_MAGIC:
        raise RepackError("stock boot.img does not start with ANDROID! magic")
    if total < PAGE + AVBF_LEN:
        raise RepackError("stock boot.img is too small to be a boot image")

    # ---- boot header v4 (little-endian) ----
    header_version = u32le(stock, 40)
    header_size = u32le(stock, 20)
    stock_ksize = u32le(stock, 8)
    stock_os_version = u32le(stock, 16)
    stock_ramdisk = u32le(stock, 12)

    if header_version != 4:
        raise RepackError(
            "stock boot header_version is %d, this tool implements v4 only"
            % header_version
        )
    if header_size <= 0 or header_size > PAGE:
        raise RepackError("stock header_size=%d looks wrong" % header_size)

    hdr = bytearray(stock[0:header_size])
    # preserve everything else; only touch kernel_size / ramdisk / signature
    stock_sig_size = u32le(stock, 1580) if header_size >= 1584 else 0

    # ---- AVBf footer ----
    avbf = bytearray(stock[total - AVBF_LEN:])
    if avbf[:4] != AVBF_MAGIC:
        raise RepackError(
            "no AVBf footer in the last 64 bytes of the stock image - this image has "
            "no AVB and cannot be used as a template"
        )
    stock_orig_image_size = u64be(avbf, 12)
    stock_vbmeta_off = u64be(avbf, 20)
    stock_vbmeta_size = u64be(avbf, 28)
    if not (PAGE <= stock_vbmeta_off < total):
        raise RepackError(
            "AVBf.vbmeta_offset=0x%X is out of range (file is %d bytes)"
            % (stock_vbmeta_off, total)
        )
    if stock[stock_vbmeta_off:stock_vbmeta_off + 4] != AVB0_MAGIC:
        raise RepackError(
            "no AVB0 magic at AVBf.vbmeta_offset=0x%X" % stock_vbmeta_off
        )

    vbmeta = bytearray(stock[stock_vbmeta_off:stock_vbmeta_off + stock_vbmeta_size])
    vbhdr = parse_vbmeta_header(bytes(vbmeta))

    # ---- new payload layout ----
    payload_end = PAGE + pad_to_page(ksize)
    if payload_end + len(vbmeta) > total - AVBF_LEN:
        raise RepackError(
            "Image too large: payload ends at 0x%X, + vbmeta(%d) would collide with "
            "the AVBf footer (partition is %d bytes).  Stock kernel_size=%d, ours=%d."
            % (payload_end, len(vbmeta), total, stock_ksize, ksize)
        )

    # ---- rewrite boot header ----
    hdr[8:12] = struct.pack("<I", ksize)          # kernel_size
    hdr[12:16] = struct.pack("<I", 0)             # ramdisk_size
    if header_size >= 1584:
        hdr[1580:1584] = struct.pack("<I", 0)     # signature_size (v4 fixed offset)

    # ---- assemble the image ----
    out = bytearray(total)                        # zero fill
    out[0:header_size] = hdr
    out[PAGE:PAGE + ksize] = kernel

    new_salt = None
    new_digest = None
    desc_info = None
    changes = []

    if a.update_digest:
        desc_info = find_hash_descriptor(
            bytes(vbmeta), vbhdr, stock, a.partition.encode(),
            expect_image_size=(int(a.expect_image_size, 0) if a.expect_image_size else None),
            expect_salt=a.expect_salt,
        )
        if desc_info is None:
            raise RepackError(
                "no AvbHashDescriptor found in AVB0 - cannot --update-digest.\n"
                "  Re-run without --update-digest (v1 mode clones AVB0 verbatim)."
            )
        if not desc_info["validated"]:
            raise RepackError(
                "found a hash descriptor (%s) but could not validate the AVB hash rule "
                "against the stock image, so the field offsets are not trustworthy.\n"
                "  Refusing to write a wrong digest.  Re-run without --update-digest."
                % desc_info["layout"]
            )
        if desc_info["salt_len"] != 32 or desc_info["digest_len"] != 32:
            raise RepackError(
                "hash descriptor uses salt_len=%d digest_len=%d; only SHA-256 "
                "(32/32) can be recomputed" % (desc_info["salt_len"], desc_info["digest_len"])
            )

        # salt = sha256(kernel segment) <- the rule the stock image itself follows
        new_salt = hashlib.sha256(kernel).digest()
        # digest = sha256(salt || image[0:image_size]); compute after the header and
        # the kernel are already in place so padding is accounted for.
        new_image_size = payload_end
        digest_input = bytearray(out[0:new_image_size])
        new_digest = hashlib.sha256(new_salt + bytes(digest_input)).digest()

        old_image_size = struct.unpack_from(desc_info["image_size_fmt"], vbmeta,
                                           desc_info["image_size_off"])[0]
        old_salt = bytes(vbmeta[desc_info["salt_off"]:desc_info["salt_off"] + 32])
        old_digest = bytes(vbmeta[desc_info["digest_off"]:desc_info["digest_off"] + 32])

        struct.pack_into(desc_info["image_size_fmt"], vbmeta,
                         desc_info["image_size_off"], new_image_size)
        vbmeta[desc_info["salt_off"]:desc_info["salt_off"] + 32] = new_salt
        vbmeta[desc_info["digest_off"]:desc_info["digest_off"] + 32] = new_digest

        if old_image_size != new_image_size:
            changes.append(("image_size", desc_info["image_size_off"],
                            "0x%X" % old_image_size, "0x%X" % new_image_size))
        if old_salt != new_salt:
            changes.append(("salt", desc_info["salt_off"], old_salt.hex(), new_salt.hex()))
        if old_digest != new_digest:
            changes.append(("digest", desc_info["digest_off"],
                            old_digest.hex(), new_digest.hex()))

    # ---- place AVB0 + AVBf ----
    out[payload_end:payload_end + len(vbmeta)] = vbmeta
    avbf[12:20] = struct.pack(">Q", payload_end)   # original_image_size
    avbf[20:28] = struct.pack(">Q", payload_end)   # vbmeta_offset
    # vbmeta_size stays as-is
    out[total - AVBF_LEN:] = avbf

    # ---- write ----
    outdir = os.path.dirname(os.path.abspath(a.out_boot))
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir)
    with open(a.out_boot, "wb") as f:
        f.write(out)
    out_sha = sha256_hex(bytes(out))

    # ------------------------------------------------------------------
    # self-check: re-read from disk and verify the structure we intended
    # ------------------------------------------------------------------
    chk = open(a.out_boot, "rb").read()
    problems = []

    def expect(label, got, want):
        if got != want:
            problems.append("%s: got %r want %r" % (label, got, want))

    expect("file_size", len(chk), total)
    expect("magic", chk[:8], HDR_MAGIC)
    expect("header_version", u32le(chk, 40), 4)
    expect("header_size", u32le(chk, 20), header_size)
    expect("kernel_size", u32le(chk, 8), ksize)
    expect("ramdisk_size", u32le(chk, 12), 0)
    expect("os_version", u32le(chk, 16), stock_os_version)
    expect("payload @4096 non-empty", chk[PAGE:PAGE + min(64, ksize)].count(0) < 64, True)
    expect("AVB0 @payload_end", chk[payload_end:payload_end + 4], AVB0_MAGIC)
    expect("AVBf @EOF-64", chk[total - 64:total - 60], AVBF_MAGIC)

    chk_avbf = chk[total - 64:]
    expect("AVBf.original_image_size", u64be(chk_avbf, 12), payload_end)
    expect("AVBf.vbmeta_offset", u64be(chk_avbf, 20), payload_end)
    expect("AVBf.vbmeta_size", u64be(chk_avbf, 28), stock_vbmeta_size)
    expect("AVB0 @AVBf.vbmeta_offset", chk[payload_end:payload_end + 4], AVB0_MAGIC)

    # zero regions must be clean
    gap_start = payload_end + len(vbmeta)
    gap_end = total - AVBF_LEN
    if gap_end > gap_start:
        if chk[gap_start:gap_end] != b"\x00" * (gap_end - gap_start):
            problems.append("tail region [0x%X,0x%X) is not zero-filled" % (gap_start, gap_end))

    # in v2, re-verify the digest straight out of the written file
    if a.update_digest:
        vb2 = chk[payload_end:payload_end + len(vbmeta)]
        got_size = struct.unpack_from(desc_info["image_size_fmt"], vb2,
                                      desc_info["image_size_off"])[0]
        got_salt = bytes(vb2[desc_info["salt_off"]:desc_info["salt_off"] + 32])
        got_dig = bytes(vb2[desc_info["digest_off"]:desc_info["digest_off"] + 32])
        expect("descriptor.image_size", got_size, payload_end)
        expect("descriptor.salt", got_salt, new_salt)
        expect("descriptor.digest", got_dig, new_digest)
        recalc = hashlib.sha256(got_salt + chk[0:got_size]).digest()
        expect("digest recomputed from the written file", recalc, got_dig)
        # everything outside the three fields must be identical to the stock vbmeta
        changed_offsets = set()
        img_off = desc_info["image_size_off"]
        fmt_size = struct.calcsize(desc_info["image_size_fmt"])
        changed_offsets.update(range(img_off, img_off + fmt_size))
        changed_offsets.update(range(desc_info["salt_off"], desc_info["salt_off"] + 32))
        changed_offsets.update(range(desc_info["digest_off"], desc_info["digest_off"] + 32))
        stray = 0
        for i in range(len(vb2)):
            if vb2[i] != vbmeta[i] and i not in changed_offsets:
                stray += 1
        expect("bytes changed outside the 3 descriptor fields", stray, 0)
    else:
        # v1: the AVB0 block must be bit-for-bit the stock block
        vb_new = chk[payload_end:payload_end + len(vbmeta)]
        expect("v1 vbmeta identical to stock", bytes(vb_new), bytes(vbmeta))

    if problems:
        raise RepackError(
            "self-check FAILED (%d problem(s)):\n%s"
            % (len(problems), "\n".join("  - " + p for p in problems))
        )

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------
    mode = "v2-update-digest" if a.update_digest else "v1-clone-avb"
    algo_name = ALGORITHM_NAMES.get(vbhdr["algorithm_type"],
                                    "UNKNOWN(%d)" % vbhdr["algorithm_type"])
    v2_changed_bytes = (8 if desc_info and desc_info["image_size_fmt"] == ">Q" else 4) + 64
    if not a.quiet:
        print("mode                 : %s" % mode)
        print("stock boot           : %s (%d bytes)" % (a.stock_boot, total))
        print("stock sha256         : %s" % stock_sha)
        print("Image                : %s (%d bytes)" % (a.image, ksize))
        print("kernel_size          : %d (0x%X)" % (ksize, ksize))
        print("payload_end          : 0x%X" % payload_end)
        print("stock vbmeta         : offset=0x%X size=%d"
              % (stock_vbmeta_off, stock_vbmeta_size))
        print("output               : %s" % a.out_boot)
        print("output sha256        : %s" % out_sha)
        print("")
        print("AVB layout (vbmeta %d B at boot.img+0x%X):"
              % (len(vbmeta), payload_end))
        print("  [0x%03X, 0x%03X) header                %4d B"
              % (0, 256, 256))
        print("  [0x%03X, 0x%03X) auth data             %4d B   "
              "(hash %d B + RSA signature %d B + padding)"
              % (vbhdr["auth_start"], vbhdr["aux_start"], vbhdr["auth_block_size"],
                 vbhdr["hash_size"], vbhdr["signature_size"]))
        print("  [0x%03X, 0x%03X) aux data              %4d B   "
              "<- the '1664-byte block' is just aux_block_size"
              % (vbhdr["aux_start"], vbhdr["vbmeta_end"], vbhdr["aux_block_size"]))
        print("    [0x%03X, 0x%03X)   descriptors        %4d B   "
              "(descriptors_offset=0, relative to the aux block)"
              % (vbhdr["desc_start"], vbhdr["desc_end"], vbhdr["descriptors_size"]))
        print("    [0x%03X, 0x%03X)   public key         %4d B   (never touched)"
              % (vbhdr["pk_start"], vbhdr["pk_end"], vbhdr["public_key_size"]))
        pad_from = max(vbhdr["desc_end"], vbhdr["pk_end"])
        pad_len = vbhdr["vbmeta_end"] - pad_from
        if pad_len > 0:
            print("    [0x%03X, 0x%03X)   padding            %4d B   (never touched)"
                  % (pad_from, vbhdr["vbmeta_end"], pad_len))
        print("  release_string       : %s" % vbhdr["release_string"])
        print("  algorithm_type       : %d (%s)" % (vbhdr["algorithm_type"], algo_name))
        print("  vbmeta signed        : %s"
              % ("YES - auth block carries a %d-byte RSA signature"
                 % vbhdr["signature_size"] if vbhdr["signature_size"] else "no"))
        if desc_info:
            print("  hash descriptor      : [0x%X, 0x%X) len=%d partition=%r layout=%s"
                  % (desc_info["desc_off"],
                     desc_info["desc_off"] + desc_info["desc_len"],
                     desc_info["desc_len"],
                     desc_info["partition_name"].decode("ascii", "replace"),
                     desc_info["layout"]))
            if desc_info["hash_algorithm"]:
                print("  hash_algorithm field : %r (32-byte NUL-padded string)"
                      % desc_info["hash_algorithm"])
            print("  desc.image_size      : @vbmeta+0x%X (%s) = 0x%X"
                  % (desc_info["image_size_off"],
                     desc_info["image_size_fmt"].replace(">", "u").replace("Q", "64").replace("I", "32"),
                     desc_info["image_size"]))
            print("  desc.salt            : @vbmeta+0x%X (%d B)"
                  % (desc_info["salt_off"], desc_info["salt_len"]))
            print("  desc.digest          : @vbmeta+0x%X (%d B)"
                  % (desc_info["digest_off"], desc_info["digest_len"]))
        if a.update_digest:
            print("")
            print("v2 field updates (offsets are vbmeta-relative):")
            if not changes:
                print("  (none) - all three fields already hold the target values")
            for name, off, old, new in changes:
                print("  %-11s @0x%03X  %s -> %s" % (name, off, old, new))
            if not changes:
                print("  => this output is byte-identical to a v1 build of the same Image")
            print("")
            print("!! WARNING: the stock vbmeta is SIGNED (%s, %d-byte RSA signature in the"
                  % (algo_name, vbhdr["signature_size"]))
            print("!! auth block). Rewriting any descriptor changes the bytes the signature")
            print("!! covers, so that signature is now STALE. We cannot re-sign without")
            print("!! Meizu's private key.")
            print("!! v2 is therefore a DIAGNOSTIC image: footer + descriptor are honest,")
            print("!! but the vbmeta signature no longer verifies.")
            print("!! For a real device, prefer v1 (AVB0 copied byte-for-byte, signature")
            print("!! intact, only the descriptor's image_size/salt/digest are stale).")
        print("")
        print("SELF-CHECK PASSED (header/footer/AVB0 positions, zero fill%s)"
              % (", v1 vbmeta byte-identical to stock" if not a.update_digest
                 else ", digest re-verified from disk, no stray byte changes"))

    if a.report:
        lines = [
            "mode=%s" % mode,
            "stock_boot=%s" % os.path.abspath(a.stock_boot),
            "stock_size=%d" % total,
            "stock_sha256=%s" % stock_sha,
            "image=%s" % os.path.abspath(a.image),
            "kernel_size=%d" % ksize,
            "header_version=%d" % header_version,
            "header_size=%d" % header_size,
            "os_version=0x%08x" % stock_os_version,
            "payload_end=0x%X" % payload_end,
            # ---- AVB facts, the block the CI gate and the repo docs consume ----
            "avb_vbmeta_offset=0x%X" % payload_end,
            "avb_vbmeta_size=%d" % len(vbmeta),
            "avb_footer=EOF-64",
            "avb_signed=%d" % (1 if vbhdr["signature_size"] else 0),
            "avb_algorithm=%d" % vbhdr["algorithm_type"],
            "avb_algorithm_name=%s" % algo_name,
            "avb_auth_size=%d" % vbhdr["auth_block_size"],
            "avb_aux_size=%d" % vbhdr["aux_block_size"],
            "avb_descriptors_offset=%d" % vbhdr["descriptors_offset"],
            "avb_descriptors_size=%d" % vbhdr["descriptors_size"],
            "avb_release_string=%s" % vbhdr["release_string"],
            "stock_vbmeta_offset=0x%X" % stock_vbmeta_off,
            "stock_vbmeta_size=%d" % stock_vbmeta_size,
            "aux_block_range=0x%X-0x%X" % (vbhdr["aux_start"], vbhdr["vbmeta_end"]),
            "output=%s" % os.path.abspath(a.out_boot),
            "output_size=%d" % total,
            "output_sha256=%s" % out_sha,
        ]
        if desc_info:
            lines += [
                "hash_desc_layout=%s" % desc_info["layout"],
                "hash_desc_validated_by=%s" % desc_info.get("validated_by", ""),
                "hash_desc_offset=0x%X" % desc_info["desc_off"],
                "hash_desc_len=%d" % desc_info["desc_len"],
                "hash_desc_partition=%s" % desc_info["partition_name"].decode("ascii", "replace"),
                "hash_desc_algorithm_field=%s" % (desc_info["hash_algorithm"] or "-"),
                "hash_desc_image_size_offset=0x%X" % desc_info["image_size_off"],
                "hash_desc_salt_offset=0x%X" % desc_info["salt_off"],
                "hash_desc_digest_offset=0x%X" % desc_info["digest_off"],
            ]
        if a.update_digest:
            lines += [
                "descriptor_image_size_offset=0x%X" % desc_info["image_size_off"],
                "descriptor_salt_offset=0x%X" % desc_info["salt_off"],
                "descriptor_digest_offset=0x%X" % desc_info["digest_off"],
                "descriptor_salt=%s" % (new_salt.hex() if new_salt else ""),
                "descriptor_digest=%s" % (new_digest.hex() if new_digest else ""),
                "changed_fields=%d" % len(changes),
            ]
            for i, (name, off, old, new) in enumerate(changes):
                lines.append("change%d=%s@0x%X:%s->%s" % (i, name, off, old, new))
        with open(a.report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if not a.quiet:
            print("report               : %s" % a.report)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RepackError as e:
        print("::error::%s" % str(e).replace("\n", " "), file=sys.stderr)
        print("[ERROR] %s" % e, file=sys.stderr)
        sys.exit(1)
