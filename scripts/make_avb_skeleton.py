#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the "AVB skeleton" template for Meizu 21 (M2461) from a stock boot.img.

GENERATION COMMAND (how the committed skeleton was produced)
------------------------------------------------------------
    python3 make_avb_skeleton.py \\
        "C:/MEIZU21_12.7/boot.img" \\
        "avb/stock_boot_skeleton.img.zlib" \\
        --device meizu21 --model M2461 --soc SM8650 --flyme "Flyme 12.7.0.0A"

    # then verify:
    python3 repack_avb_boot.py avb/stock_Image.bin "C:/MEIZU21_12.7/boot.img" \\
        avb/rt_v1.img
    # avb/rt_v1.img sha256 must equal the stock boot.img sha256

WHY A SKELETON
--------------
``repack_avb_boot.py`` needs a real stock boot.img as the AVB source of truth (the
AVB0 offset moves between Flyme releases, so it cannot be hard-coded).  But a 96 MiB
binary is not something you want in git, so this script strips it down:

  * zero the kernel payload region          [4096, vbmeta_offset)
  * zero the partition tail                 [vbmeta_offset + vbmeta_size, EOF-64)
  * keep the 4096-byte boot header, the AVB0 block and the trailing 64-byte AVBf
    footer byte-for-byte
  * zlib-compress the result               96 MiB -> ~98 KiB

Every byte offset of the original image survives, so the repacker can still resolve
AVB0 dynamically from the AVBf footer exactly as it would with the full image.

DEVICE GUARD
------------
Meizu 21 (M2461) and Meizu 21 Pro (M2481) boot images are byte-for-byte identical in
every structural field: same 100,663,296 size, same header v4/1584, same
os_version 0x1c00017a, same vbmeta_offset 0x2054000 and size 2496.  Size and field
checks therefore cannot tell them apart - only the stock image sha256 can.  Applying
the wrong device's AVB0 is a hard brick risk, so this script refuses to run unless
the input sha256 is on the whitelist for the requested --device.

Standard library only.
"""

import argparse
import hashlib
import os
import struct
import sys
import zlib

PAGE = 4096
HDR_MAGIC = b"ANDROID!"
AVB0_MAGIC = b"AVB0"
AVBF_MAGIC = b"AVBf"
AVBF_LEN = 64

# ---------------------------------------------------------------------------
# Device registry: which stock boot.img sha256 belongs to which device.
# Prefixes (not full digests) so a re-released Flyme build with the same boot.img
# is accepted, while a different Flyme version is not silently mixed up.
# ---------------------------------------------------------------------------
DEVICE_REGISTRY = {
    "meizu21": {
        "model": "M2461",
        "soc": "SM8650",
        "stock_sha256": {
            # Flyme 12.6
            "12a0a75750e2fdb0": "Flyme 12.6",
            # Flyme 12.7.0.0A  <- the one this skeleton is built from
            "8c899eb3b7fcb65e": "Flyme 12.7.0.0A",
        },
    },
    "meizu21pro": {
        "model": "M2481",
        "soc": "SM8650",
        "stock_sha256": {
            "ebdd3dc2568c76c3": "Flyme 12.6",
        },
    },
}

# sha256 of images that are known to be the *other* device, reported with a hint.
FOREIGN_HINTS = {
    "12a0a75750e2fdb0": "meizu21 (M2461) Flyme 12.6",
    "8c899eb3b7fcb65e": "meizu21 (M2461) Flyme 12.7.0.0A",
    "ebdd3dc2568c76c3": "meizu21pro (M2481) Flyme 12.6",
}


class SkeletonError(Exception):
    pass


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def u32le(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def u32be(buf, off):
    return struct.unpack_from(">I", buf, off)[0]


def u64be(buf, off):
    return struct.unpack_from(">Q", buf, off)[0]


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Strip a stock Meizu boot.img down to a zlib-compressed AVB "
                    "skeleton (header + AVB0 + AVBf, payload zeroed)."
    )
    ap.add_argument("stock_boot", help="stock boot.img")
    ap.add_argument("out_zlib", help="output .img.zlib")
    ap.add_argument("--device", default="meizu21", choices=sorted(DEVICE_REGISTRY),
                    help="device key; guards against mixing up M2461/M2481")
    ap.add_argument("--model", default="", help="model string, must match the registry")
    ap.add_argument("--soc", default="", help="SoC string for source.txt")
    ap.add_argument("--flyme", default="", help="Flyme version string for source.txt")
    ap.add_argument("--force", action="store_true",
                    help="skip the sha256 whitelist (NOT recommended; only for a new "
                         "Flyme release you have verified by hand)")
    ap.add_argument("--no-source", action="store_true",
                    help="do not write source.txt next to the skeleton")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv if argv is not None else sys.argv[1:])

    if not os.path.isfile(a.stock_boot):
        raise SkeletonError("stock boot.img not found: %s" % a.stock_boot)

    info = DEVICE_REGISTRY[a.device]
    model = a.model or info["model"]
    if model != info["model"]:
        raise SkeletonError(
            "--model=%s but %s is %s" % (model, a.device, info["model"])
        )

    src_sha = sha256_of(a.stock_boot)
    whitelist = info["stock_sha256"]
    hit = next((p for p in whitelist if src_sha.startswith(p)), None)
    if hit is None:
        details = []
        for p, label in FOREIGN_HINTS.items():
            if src_sha.startswith(p):
                details.append("this sha256 belongs to %s" % label)
        if a.force:
            print("::warning::sha256 %s is not on the %s whitelist; proceeding because "
                  "--force was given" % (src_sha, a.device))
        else:
            raise SkeletonError(
                "stock sha256=%s is not on the %s (M2461) whitelist %s.\n"
                "  M2461 and M2481 boot images are structurally identical - only the "
                "sha256 can tell them apart, and applying the wrong AVB0 bricks the "
                "device.\n  %s\n"
                "  If this is a new Flyme release you have verified by hand, re-run "
                "with --force and update DEVICE_REGISTRY."
                % (src_sha, a.device, list(whitelist),
                   "; ".join(details) if details else "(no foreign match)")
            )

    raw = bytearray(open(a.stock_boot, "rb").read())
    total = len(raw)
    if raw[:8] != HDR_MAGIC:
        raise SkeletonError("not a boot image (magic is not ANDROID!): %s" % a.stock_boot)
    if total < PAGE + AVBF_LEN:
        raise SkeletonError("file too small to be a boot image")

    kernel_size = u32le(raw, 8)
    header_size = u32le(raw, 20)
    header_version = u32le(raw, 40)
    os_version = u32le(raw, 16)

    if raw[total - AVBF_LEN:total - AVBF_LEN + 4] != AVBF_MAGIC:
        raise SkeletonError(
            "the last 64 bytes are not an AVBf footer - this image carries no AVB and "
            "cannot be used as a template.\n  Make sure this is the *stock* boot.img "
            "and not a boot.img we produced ourselves."
        )

    avbf = bytes(raw[total - AVBF_LEN:])
    orig_image_size = u64be(avbf, 12)
    vbmeta_off = u64be(avbf, 20)
    vbmeta_size = u64be(avbf, 28)

    if not (PAGE <= vbmeta_off < total):
        raise SkeletonError(
            "AVBf.vbmeta_offset=0x%X out of range (file is %d bytes)" % (vbmeta_off, total)
        )
    if raw[vbmeta_off:vbmeta_off + 4] != AVB0_MAGIC:
        raise SkeletonError(
            "no AVB0 magic at vbmeta_offset=0x%X - the stock image looks corrupt"
            % vbmeta_off
        )
    if vbmeta_off + vbmeta_size > total - AVBF_LEN:
        raise SkeletonError(
            "AVB0 block [0x%X,0x%X) overlaps the AVBf footer"
            % (vbmeta_off, vbmeta_off + vbmeta_size)
        )

    payload_end = PAGE + ((kernel_size + PAGE - 1) // PAGE) * PAGE
    if payload_end > vbmeta_off:
        raise SkeletonError(
            "kernel payload (ends 0x%X) overruns AVB0 (0x%X)" % (payload_end, vbmeta_off)
        )

    # ---- zero the two dead regions ----
    zeroed = 0
    if vbmeta_off > PAGE:
        n = vbmeta_off - PAGE
        raw[PAGE:vbmeta_off] = b"\x00" * n
        zeroed += n
    gap_start = vbmeta_off + vbmeta_size
    gap_end = total - AVBF_LEN
    if gap_end > gap_start:
        n = gap_end - gap_start
        raw[gap_start:gap_end] = b"\x00" * n
        zeroed += n

    if len(raw) != total:
        raise SkeletonError("internal error: length changed after zeroing")

    skeleton = bytes(raw)
    blob = zlib.compress(skeleton, 9)
    outdir = os.path.dirname(os.path.abspath(a.out_zlib))
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir)
    with open(a.out_zlib, "wb") as f:
        f.write(blob)

    skeleton_sha = hashlib.sha256(skeleton).hexdigest()
    blob_sha = hashlib.sha256(blob).hexdigest()

    # ---- self-check: the skeleton must round-trip and keep every structural byte ----
    back = zlib.decompress(blob)
    problems = []
    if len(back) != total:
        problems.append("decompressed length %d != %d" % (len(back), total))
    if back[:8] != HDR_MAGIC:
        problems.append("header magic lost")
    if back[0:header_size] != bytes(raw[0:header_size]):
        problems.append("boot header changed")
    if back[vbmeta_off:vbmeta_off + vbmeta_size] != skeleton[vbmeta_off:vbmeta_off + vbmeta_size]:
        problems.append("AVB0 block changed")
    if back[total - AVBF_LEN:] != avbf:
        problems.append("AVBf footer changed")
    if back[PAGE:vbmeta_off].count(0) != vbmeta_off - PAGE:
        problems.append("payload region is not fully zeroed")
    if gap_end > gap_start and back[gap_start:gap_end].count(0) != gap_end - gap_start:
        problems.append("partition tail is not fully zeroed")
    if problems:
        raise SkeletonError("skeleton self-check FAILED: %s" % "; ".join(problems))

    print("source boot        : %s (%d bytes)" % (a.stock_boot, total))
    print("source sha256      : %s%s"
          % (src_sha, "  [whitelisted: %s]" % whitelist[hit] if hit else "  [FORCED]"))
    print("boot header        : version=%d header_size=%d kernel_size=%d os_version=0x%08x"
          % (header_version, header_size, kernel_size, os_version))
    print("payload_end        : 0x%X" % payload_end)
    print("AVBf               : original_image_size=0x%X vbmeta_offset=0x%X "
          "vbmeta_size=%d" % (orig_image_size, vbmeta_off, vbmeta_size))
    print("zeroed regions     : %d bytes total" % zeroed)
    print("  payload          : [0x%X, 0x%X) %d bytes"
          % (PAGE, vbmeta_off, vbmeta_off - PAGE))
    print("  tail             : [0x%X, 0x%X) %d bytes"
          % (gap_start, gap_end, max(0, gap_end - gap_start)))
    print("output             : %s" % a.out_zlib)
    print("output size        : %d bytes (zlib -9, from %d)" % (len(blob), total))
    print("output sha256      : %s" % blob_sha)
    print("skeleton sha256    : %s  (decompressed, 96 MiB)" % skeleton_sha)
    print("SELF-CHECK PASSED (header/AVB0/AVBf preserved, payload zeroed, zlib round-trip ok)")

    if not a.no_source:
        sdir = os.path.dirname(os.path.abspath(a.out_zlib)) or "."
        sfile = os.path.join(sdir, "source.txt")
        soc = a.soc or info["soc"]

        # ---- parse just enough of the AVB0 header to record the AVB facts ----
        vb = bytes(raw[vbmeta_off:vbmeta_off + vbmeta_size])
        auth_size = u64be(vb, 12)
        aux_size = u64be(vb, 20)
        algorithm = u32be(vb, 28)
        sig_size = u64be(vb, 56)
        desc_off = u64be(vb, 96)      # relative to the AUX block, not the vbmeta
        desc_size = u64be(vb, 104)
        # descriptor[0] must be the hash descriptor (tag 2); detect the layout by
        # looking at the 32-byte field at +0x18 - "sha256" means the legacy string
        # layout, a small integer means the modern enum layout.
        desc_abs = 256 + auth_size + desc_off
        hash_desc_layout = "unknown"
        if vb[desc_abs:desc_abs + 8] == b"\x00\x00\x00\x00\x00\x00\x00\x02":
            # legacy layout stores a NUL-padded ASCII algorithm name at +0x18;
            # the modern layout stores a uint32 enum there instead.
            algo_field = vb[desc_abs + 0x18:desc_abs + 0x38].split(b"\x00")[0]
            printable = all(0x20 <= c < 0x7F for c in algo_field) and len(algo_field) > 0
            if printable and algo_field.lower() in (b"sha256", b"sha512"):
                hash_desc_layout = "legacy-string32"
            else:
                hash_desc_layout = "modern-enum"

        with open(sfile, "w", encoding="utf-8", newline="\n") as f:
            f.write("# AVB skeleton provenance - generated by scripts/make_avb_skeleton.py\n")
            f.write("# CI MUST cross-check both device= and boot_sha256= before using "
                    "this template.\n")
            f.write("# AVB facts below were measured on the source boot.img, not assumed.\n")
            f.write("device=%s\n" % a.device)
            f.write("model=%s\n" % model)
            f.write("soc=%s\n" % soc)
            if a.flyme:
                f.write("flyme=%s\n" % a.flyme)
            f.write("boot_sha256=%s\n" % src_sha)
            f.write("boot_size=%d\n" % total)
            f.write("header_version=%d\n" % header_version)
            f.write("header_size=%d\n" % header_size)
            f.write("kernel_size=%d\n" % kernel_size)
            f.write("payload_end=0x%X\n" % payload_end)
            f.write("avb_vbmeta_offset=%d\n" % vbmeta_off)
            f.write("avb_vbmeta_offset_hex=0x%X\n" % vbmeta_off)
            f.write("avb_vbmeta_size=%d\n" % vbmeta_size)
            f.write("avb_footer=EOF-64\n")
            f.write("avb_footer_original_image_size=0x%X\n" % orig_image_size)
            f.write("avb_signed=%d\n" % (1 if sig_size else 0))
            f.write("avb_algorithm=%d\n" % algorithm)
            f.write("avb_auth_size=%d\n" % auth_size)
            f.write("avb_aux_size=%d\n" % aux_size)
            f.write("avb_descriptors_offset=%d\n" % desc_off)
            f.write("avb_descriptors_size=%d\n" % desc_size)
            f.write("avb_desc_off=%d\n" % desc_off)
            f.write("avb_desc_size=%d\n" % desc_size)
            f.write("avb_desc_abs_offset=0x%X\n" % desc_abs)
            f.write("hash_desc_layout=%s\n" % hash_desc_layout)
            f.write("skeleton_len=%d\n" % len(blob))
            f.write("skeleton_sha256=%s\n" % blob_sha)
            f.write("skeleton_raw_sha256=%s\n" % skeleton_sha)
            f.write("generated_by=scripts/make_avb_skeleton.py\n")
            f.write("generated_from=%s\n" % os.path.basename(a.stock_boot))
        print("source manifest    : %s" % sfile)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SkeletonError as e:
        print("::error::%s" % str(e).replace("\n", " "), file=sys.stderr)
        print("[ERROR] %s" % e, file=sys.stderr)
        sys.exit(1)
