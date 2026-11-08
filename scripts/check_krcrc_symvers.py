#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KMI export audit + optional binary __kcrctab lock for the Meizu 21 (M2461) GKI kernel.

Meizu ships 361 vendor modules with CONFIG_MODVERSIONS=y.  A module loads only if
*every* symbol it NEEDs is exported by the kernel AND the 32-bit CRC of that symbol
matches the one the module was built against.  Otherwise the module is rejected with
``disagrees about version`` and the device dies on the first screen because a
vendor_dlkm/system_dlkm module never comes up.

This script reports three things, with deliberately different severities:

  (1) Missing stock symbols            INFORMATIONAL
      Symbols present in the stock Module.symvers but absent from the built one.
      This number can never reach 0 and that is fine: the stock symvers is also a
      historical ledger of symbols Meizu once exported.  On the M2461 line the
      steady state is "Missing 6", and those 6 were verified to be neither NEED nor
      DEF in module_kmi_manifest.txt.  Do NOT chase a non-zero value.

  (2) (stock - built) INTERSECT NEED   HARD GATE
      Symbols the stock kernel exported *and* a vendor module requires, that this
      build no longer exports.  This must be empty.  A non-empty set means an
      ``Unknown symbol`` at load time and a device stuck on the boot screen.

      Note the intersection with the stock baseline is essential.  The naive test
      "NEED - built" is always non-empty: on the M2461 manifest 1401 of the 4180
      NEED symbols are not in the stock Module.symvers either, because they are
      satisfied by other vendor modules (module-to-module dependencies) or are not
      kernel-exported at all.  The stock kernel did not export them, so their
      absence from our build is not a regression.  Only a symbol the *stock* kernel
      exported is a symbol our kernel is on the hook for.

  (3) Raw CRC matches                  INFORMATIONAL
      How many stock symbols also have an identical CRC in the build.

Additionally (informational unless --strict-need-crc) the NEED CRC values from the
manifest are cross-checked against the stock and built CRCs; a mismatch there is what
``--rewrite`` exists to repair.

USAGE
-----
    python3 check_krcrc_symvers.py <built Module.symvers> <stock_Module.symvers> \\
                                   <module_kmi_manifest.txt> [Image] [options]

    --rewrite             patch the 32-bit CRCs inside the Image's __kcrctab so they
                          equal the stock values.  OFF BY DEFAULT: on the M2461 build
                          the string-anchored table locator failed ("Could not locate
                          struct kernel_symbol for 'module_layout'"), so the working
                          configuration relied on the source-level CRCs being right.
                          Only enable after confirming the locator works on your image.
    --strict-need-crc     exit 1 if any NEED symbol's CRC disagrees
    --require-export SYM  extra symbol that must be exported (repeatable)
    --json PATH           machine-readable result

EXIT CODES
----------
    0  missing ∩ NEED is empty (and, with --strict-need-crc, all NEED CRCs agree)
    1  hard gate failed
    2  tooling / input problem

Standard library only.
"""

import argparse
import json
import os
import struct
import sys


class AuditError(Exception):
    pass


def die(msg, code=2):
    print("[ERROR] %s" % msg, file=sys.stderr)
    sys.exit(code)


def load_symvers(path):
    """Parse a Module.symvers: '<0xCRC>\\t<name>\\t<module>\\t<export_type>'."""
    syms = {}
    if not os.path.isfile(path):
        raise AuditError("Module.symvers not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                crc = int(parts[0], 16)
            except ValueError:
                continue
            syms[parts[1]] = crc
    if not syms:
        raise AuditError("no symbols parsed from %s - wrong format?" % path)
    return syms


def load_manifest(path):
    """Parse module_kmi_manifest.txt.

    Format (one record per line, '#' comments):
        NEED 0x1b75e0a2 FOD_switch_enable
        DEF AIN_X
    """
    need = {}
    define = set()
    if not os.path.isfile(path):
        raise AuditError("KMI manifest not found: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            kind = parts[0].upper()
            if kind == "NEED":
                if len(parts) < 3:
                    continue
                name = parts[2]
                try:
                    crc = int(parts[1], 16)
                except ValueError:
                    crc = None
                need[name] = crc
            elif kind == "DEF":
                if len(parts) >= 2:
                    define.add(parts[1])
    if not need and not define:
        raise AuditError("no NEED/DEF records parsed from %s" % path)
    return need, define


def sync_image_kcrctab(img_path, stock_crcs, verbose=True):
    """Rewrite the __kcrctab entries in Image to the stock CRC values.

    Ported from the m2481 kit implementation.  The locator is a string heuristic
    ("\\0module_layout\\0" -> walk back over the PREF32 relative offsets to find
    struct kernel_symbol entries), which is why this is opt-in: it failed to locate
    the table on one M2461 build.  Returns (patched, total, ok).
    """
    with open(img_path, "rb") as f:
        img = bytearray(f.read())

    target = img.find(b"\x00module_layout\x00")
    if target == -1:
        print("[WARN] 'module_layout' string not found in Image; skipping __kcrctab sync.")
        return 0, 0, False
    target += 1

    hit_entry = None
    search_start = max(0, target - 0x600000)
    for pos in range(search_start, target, 4):
        rel = struct.unpack_from("<i", img, pos)[0]
        if pos + rel == target:
            hit_entry = pos - 4
            break
    if hit_entry is None:
        print("[WARN] Could not locate struct kernel_symbol for 'module_layout' in Image.")
        return 0, 0, False

    str_min = target - 0x40000
    str_max = target + 0x80000

    def is_valid_entry(off):
        if off < 0 or off + 12 > len(img):
            return False
        _, n_off, ns_off = struct.unpack_from("<iii", img, off)
        n_ptr = off + 4 + n_off
        if not (str_min <= n_ptr <= str_max):
            return False
        if ns_off != 0:
            ns_ptr = off + 8 + ns_off
            if not (str_min <= ns_ptr <= str_max):
                return False
        return True

    start = hit_entry
    while is_valid_entry(start - 12):
        start -= 12
    end = hit_entry
    while is_valid_entry(end + 12):
        end += 12
    end += 12
    count = (end - start) // 12
    crc_start = end

    if count <= 0 or crc_start + count * 4 > len(img):
        print("[WARN] __ksymtab walk produced a nonsensical table (count=%d); "
              "skipping __kcrctab sync." % count)
        return 0, 0, False

    patched = 0
    for i in range(count):
        off = start + i * 12
        _, n_off, _ = struct.unpack_from("<iii", img, off)
        n_ptr = off + 4 + n_off
        end_str = img.find(b"\x00", n_ptr)
        name = bytes(img[n_ptr:end_str]).decode("ascii", errors="replace")
        cur = struct.unpack_from("<I", img, crc_start + i * 4)[0]
        if name in stock_crcs and stock_crcs[name] != cur:
            struct.pack_into("<I", img, crc_start + i * 4, stock_crcs[name])
            patched += 1

    if verbose:
        print("  __ksymtab entries located : %d (crc table at file offset 0x%X)"
              % (count, crc_start))
    if patched:
        with open(img_path, "wb") as f:
            f.write(img)
        print("[OK] binary __kcrctab sync: locked %d of %d symbol CRCs to stock values in %s"
              % (patched, count, img_path))
    else:
        print("[OK] binary __kcrctab already matches the stock CRCs (%d entries)" % count)
    return patched, count, True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Audit KMI exports against the stock Module.symvers and the vendor "
                    "module NEED manifest."
    )
    ap.add_argument("built", help="Module.symvers produced by the build")
    ap.add_argument("stock", help="stock_Module.symvers (the CRC baseline)")
    ap.add_argument("manifest", help="module_kmi_manifest.txt (NEED / DEF ledger)")
    ap.add_argument("image", nargs="?", default="",
                    help="compiled Image (optional; required for --rewrite)")
    ap.add_argument("--rewrite", action="store_true",
                    help="patch __kcrctab inside Image to the stock CRCs (opt-in, see "
                         "module docstring)")
    ap.add_argument("--strict-need-crc", action="store_true",
                    help="fail when a NEED symbol's CRC disagrees")
    ap.add_argument("--require-export", action="append", default=[],
                    metavar="SYM", help="extra symbol that must be exported (repeatable)")
    ap.add_argument("--json", default="", help="write a JSON result file")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        built = load_symvers(a.built)
        stock = load_symvers(a.stock)
        need, define = load_manifest(a.manifest)
    except AuditError as e:
        die(str(e))

    missing_stock = sorted(set(stock) - set(built))

    # (2) the real gate.
    #
    # It is tempting to use "NEED - built", but that is wrong and always fails: 1401
    # of the M2461 manifest's 4180 NEED symbols are not in the stock Module.symvers
    # either - they are satisfied by *other vendor modules* (module-to-module deps)
    # or are simply not kernel-exported at all.  The stock kernel did not export
    # them, so their absence from our build is not a regression.
    #
    # The correct reading (and the one the reference repo settled on) is:
    #     (stock - built) INTERSECT NEED   must be empty
    # i.e. "every symbol that the stock kernel exported AND a vendor module needs is
    # still exported".  This is what actually predicts an "Unknown symbol" panic.
    missing_need = sorted(n for n in need if n in stock and n not in built)
    # informational: NEED entries the stock baseline never covered
    need_outside_stock = sorted(n for n in need if n not in stock)
    crc_match = sum(1 for k, v in stock.items() if built.get(k) == v)

    # NEED CRC cross-check against both baselines
    need_crc_vs_stock_bad = []
    need_crc_vs_built_bad = []
    for name, ncrc in need.items():
        if ncrc is None:
            continue
        if name in stock and stock[name] != ncrc:
            need_crc_vs_stock_bad.append((name, ncrc, stock[name]))
        if name in built and built[name] != ncrc:
            need_crc_vs_built_bad.append((name, ncrc, built[name]))

    print("=== KMI & krcrc Audit ===")
    print("built Module.symvers        : %s" % a.built)
    print("stock Module.symvers        : %s" % a.stock)
    print("KMI manifest                : %s" % a.manifest)
    print("")
    print("Built exported symbols      : %d" % len(built))
    print("Stock exported symbols      : %d" % len(stock))
    print("Manifest NEED / DEF         : %d / %d" % (len(need), len(define)))
    print("")
    print("(1) Missing stock symbols   : %d   [informational - can never reach 0, "
          "do not chase it]" % len(missing_stock))
    if missing_stock:
        preview = ", ".join(missing_stock[:12])
        more = "" if len(missing_stock) <= 12 else " ... (+%d)" % (len(missing_stock) - 12)
        print("    %s%s" % (preview, more))
        in_need = [s for s in missing_stock if s in need]
        if in_need:
            print("    !! %d of these ARE listed as NEED - they are gate (2) failures: %s"
                  % (len(in_need), ", ".join(in_need[:8])))
    print("(2) (stock-built) ∩ NEED    : %d   [HARD GATE - must be 0]" % len(missing_need))
    print("    NEED symbols the stock baseline never exported: %d   [informational - "
          "they come from other modules or are not kernel-exported]"
          % len(need_outside_stock))
    print("(3) Raw CRC matches         : %d / %d   [informational]"
          % (crc_match, len(stock)))
    print("")
    print("NEED CRC vs stock baseline  : %d mismatched   [informational]"
          % len(need_crc_vs_stock_bad))
    print("NEED CRC vs built baseline  : %d mismatched   [informational%s]"
          % (len(need_crc_vs_built_bad),
             ", --strict-need-crc makes this fatal" if a.strict_need_crc else ""))

    hard_fail = []

    if missing_need:
        print("")
        print("[FAIL] (2) missing ∩ NEED is NOT empty - these symbols are required by "
              "vendor modules but are not exported by this kernel:")
        for s in missing_need[:40]:
            crc = need.get(s)
            stock_crc = stock.get(s)
            extra = ""
            if crc is not None:
                extra += " need_crc=0x%08x" % crc
            if stock_crc is not None:
                extra += " stock_crc=0x%08x" % stock_crc
            print("    %-60s%s" % (s, extra))
        if len(missing_need) > 40:
            print("    ... (+%d more)" % (len(missing_need) - 40))
        print("  -> An 'Unknown symbol' at module load time means the device never leaves "
              "the boot screen.")
        print("  -> Fix by exporting these in the kernel source (see the kit patch), "
              "not by relaxing this gate.")
        hard_fail.append("missing_need")
    else:
        print("")
        print("[OK] (2) missing ∩ NEED is empty: every symbol the 361 vendor modules "
              "require is exported.")

    if a.strict_need_crc and need_crc_vs_built_bad:
        print("")
        print("[FAIL] --strict-need-crc: %d NEED symbol CRC(s) disagree with the build:"
              % len(need_crc_vs_built_bad))
        for name, want, got in need_crc_vs_built_bad[:20]:
            print("    %-56s need=0x%08x built=0x%08x" % (name, want, got))
        hard_fail.append("need_crc")

    if a.require_export:
        absent = [s for s in a.require_export if s not in built]
        if absent:
            print("")
            print("[FAIL] --require-export: %d required symbol(s) not exported:" % len(absent))
            for s in absent:
                print("    %s" % s)
            hard_fail.append("require_export")
        else:
            print("[OK] all %d --require-export symbols are exported."
                  % len(a.require_export))

    # ---- optional binary CRC lock ----
    rewrite_stats = None
    if a.rewrite:
        print("")
        if not a.image or not os.path.isfile(a.image):
            print("[ERROR] --rewrite needs an existing Image path (4th argument).")
            hard_fail.append("rewrite_no_image")
        else:
            print("=== binary __kcrctab rewrite (opt-in) ===")
            patched, total, ok = sync_image_kcrctab(a.image, stock)
            rewrite_stats = dict(patched=patched, total=total, located=ok)
            if not ok:
                print("::warning::__kcrctab locator failed - the binary CRC lock did not "
                      "run. Source-level CRCs are the only protection.")
    elif a.image and os.path.isfile(a.image):
        print("")
        print("Image provided but --rewrite not given: binary __kcrctab left untouched.")

    if hard_fail:
        print("")
        print("[KMI GATE FAILED] %s" % ", ".join(hard_fail))
        rc = 1
    else:
        print("")
        print("[KMI GATE PASSED] missing ∩ NEED empty -> the vendor modules' symbol "
              "requirements are satisfied.")
        rc = 0

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(dict(
                built=a.built, stock=a.stock, manifest=a.manifest,
                built_symbols=len(built), stock_symbols=len(stock),
                manifest_need=len(need), manifest_def=len(define),
                missing_stock=missing_stock,
                missing_need=missing_need,
                raw_crc_matches=crc_match,
                need_crc_vs_stock_bad=[dict(symbol=s, need=w, stock=g)
                                       for s, w, g in need_crc_vs_stock_bad],
                need_crc_vs_built_bad=[dict(symbol=s, need=w, built=g)
                                       for s, w, g in need_crc_vs_built_bad],
                rewrite=rewrite_stats,
                hard_fail=hard_fail,
                verdict="PASS" if rc == 0 else "FAIL",
            ), f, indent=2)
        print("json: %s" % a.json)

    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AuditError as e:
        die(str(e))
