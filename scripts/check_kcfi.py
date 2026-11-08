#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kCFI type-hash gate for the Meizu 21 (M2461) GKI kernel.

Meizu's closed-source modules hard-code the kCFI type hash they expect at an
indirect call site:

    ldr  w16, [x8, #-4]        ; load the callee's type hash
    movz w17, #<lo>
    movk w17, #<hi>, lsl #16   ; the hash the module expects
    cmp  w16, w17
    b.eq +8
    brk  #0x8228               ; CFI failure -> panic -> watchdog reset
    blr  x8

So the 4 bytes immediately preceding an instrumented function must equal the hash
the vendor module baked in.  If it does not, the device dies with a "CFI failure"
about 2-3 seconds after boot and there is no graceful degradation.

This script verifies both flavours of that contract:

  1. ``func <name> <hash>``
     Read the u32 in ``.text`` at ``symbol_addr - 4``.
     Caveat (learned the hard way): only trust the inline 4 bytes.  Do NOT use
     ``nm | grep __kcfi_typeid_<name>`` - the named symbol only exists when the
     function is referenced across translation units, so e.g. ``kfree`` has an
     inline hash but no named symbol.

  2. ``traceiter <name> <hash>``
     Vendor hook traceiters are called through a function pointer, so the hash is
     materialised at the call site rather than in a preamble.  Disassemble the
     first 0x90 bytes of the symbol and rebuild the ``w17`` immediate from its
     movz/movk pair.

USAGE
-----
    python3 check_kcfi.py <vmlinux> <clang_bin_dir> <expected.txt> [options]

    --allow-missing     treat a symbol that is absent from vmlinux as N/A instead of
                        a failure (useful on a tree that intentionally drops a hook)
    --json PATH         also dump a machine-readable result
    --quiet             only print the summary

EXPECTED FILE FORMAT
--------------------
    # comment lines start with '#'; blank lines ignored
    func      <symbol> <0xHASH | ->
    traceiter <symbol> <0xHASH | ->

``-``, ``n/a``, ``none`` or ``?`` means "no expected value" -> reported N/A and never
counts as a failure.  A ready-made table for M2461 lives next to this script in
``kcfi_expected.meizu21.txt``.

EXIT CODES
----------
    0  every entry is MATCH or N/A
    1  at least one MISMATCH (or MISSING, unless --allow-missing)
    2  tooling problem (missing vmlinux / llvm tools / bad expected file)

WHY THE CLANG VERSION MATTERS
-----------------------------
``llvm-objdump`` output formatting changes between releases.  An old LLVM (e.g. 14)
prints the ``movk w17, #0x...., lsl #16`` operand differently, the immediate no
longer parses, and the gate reports "could not find imm pair" - i.e. a *false*
failure.  A wrong toolchain is more dangerous than no toolchain, so the CI pipeline
picks the highest available clang (>= 17) and this script fails loudly on a parse
error instead of quietly passing.

Standard library only (subprocess + struct + re).
"""

import argparse
import json
import os
import re
import struct
import subprocess
import sys

NA_TOKENS = ("-", "n/a", "na", "none", "?", "null")

# movz / movk on w17, with an optional lsl shift
INSN_RE = re.compile(
    r"^\s*[0-9a-fA-F]+:\s+"                       # address
    r"(?:[0-9a-fA-F]{8}\s+)?"                     # optional raw encoding
    r"(?P<op>mov[zk])\s+w17\s*,\s*#(?P<imm>0x[0-9a-fA-F]+|\d+)"
    r"(?:\s*,\s*lsl\s*#(?P<lsl>\d+))?",
    re.IGNORECASE,
)
BRK_RE = re.compile(r"^\s*[0-9a-fA-F]+:\s+(?:[0-9a-fA-F]{8}\s+)?brk\s+#", re.IGNORECASE)


class GateError(Exception):
    pass


def die(msg, code=2):
    print("[ERROR] %s" % msg, file=sys.stderr)
    sys.exit(code)


def run(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode(
            "utf-8", "replace"
        )
    except FileNotFoundError:
        raise GateError("tool not found: %s" % cmd[0])
    except subprocess.CalledProcessError as e:
        raise GateError(
            "%s failed (exit %d): %s" % (cmd[0], e.returncode, e.output[:2000])
        )


def require_tools(clang_bin):
    tools = {}
    for name in ("llvm-nm", "llvm-readelf", "llvm-objdump"):
        path = os.path.join(clang_bin, name)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise GateError(
                "required tool missing or not executable: %s\n"
                "  pass the directory that contains llvm-nm / llvm-readelf / "
                "llvm-objdump (a kleaf prebuilt clang bin dir, clang >= 17)." % path
            )
        tools[name] = path
    return tools


def load_expected(path):
    if not os.path.isfile(path):
        raise GateError("expected table not found: %s" % path)
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                raise GateError(
                    "%s:%d bad line (want '<func|traceiter> <symbol> <hash|->'): %r"
                    % (path, lineno, raw.rstrip())
                )
            kind, sym, exp = parts[0].lower(), parts[1], parts[2]
            if kind not in ("func", "traceiter"):
                raise GateError("%s:%d unknown kind %r" % (path, lineno, parts[0]))
            if exp.lower() in NA_TOKENS:
                want = None
            else:
                try:
                    want = int(exp, 16) if not exp.lower().startswith("0x") else int(exp, 16)
                except ValueError:
                    raise GateError("%s:%d cannot parse hash %r" % (path, lineno, exp))
                if not 0 <= want <= 0xFFFFFFFF:
                    raise GateError("%s:%d hash out of range: %r" % (path, lineno, exp))
            entries.append((kind, sym, want, lineno))
    if not entries:
        raise GateError("expected table is empty: %s" % path)
    return entries


def parse_symbols(nm_out):
    syms = {}
    for line in nm_out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                syms[parts[2]] = (int(parts[0], 16), parts[1])
            except ValueError:
                continue
    return syms


def find_text_section(readelf_out):
    for line in readelf_out.splitlines():
        if " .text " not in line:
            continue
        parts = line.split()
        try:
            idx = parts.index(".text")
            addr = int(parts[idx + 2], 16)
            off = int(parts[idx + 3], 16)
            size = int(parts[idx + 4], 16)
        except (ValueError, IndexError):
            continue
        return addr, off, size
    return None


def rebuild_w17_immediate(asm):
    """Reassemble the 32-bit value loaded into w17 from its movz/movk sequence.

    Returns (value, how) or (None, reason).  Handles both encodings that appear in
    practice:
        movz w17, #lo            + movk w17, #hi, lsl #16      (most common)
        movk w17, #lo            + movk w17, #hi, lsl #16
    """
    val = None
    nbits = 0
    started = False
    for line in asm.splitlines():
        if started and BRK_RE.match(line):
            break
        m = INSN_RE.match(line)
        if not m:
            continue
        op = m.group("op").lower()
        imm = int(m.group("imm"), 0)
        lsl = int(m.group("lsl") or 0)
        shift = lsl if lsl else 0
        if op == "movz":
            val = (imm << shift) & 0xFFFFFFFF
            nbits = 16
            started = True
        else:  # movk
            if val is None:
                val = 0
                nbits = 0
            val = (val | (imm << shift)) & 0xFFFFFFFF
            nbits += 16
            started = True
        if nbits >= 32:
            return val, "movz/movk sequence"
    if started:
        return None, "incomplete movz/movk pair (only %d bits assembled)" % nbits
    return None, "no movz/movk on w17 in the first bytes of the symbol"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Verify kCFI type hashes against an expected table."
    )
    ap.add_argument("vmlinux", help="vmlinux (with symbols and .text)")
    ap.add_argument("clang_bin", help="directory containing llvm-nm/llvm-readelf/llvm-objdump")
    ap.add_argument("expected", help="expected table (see module docstring)")
    ap.add_argument("--allow-missing", action="store_true",
                    help="treat an absent symbol as N/A instead of a failure")
    ap.add_argument("--json", default="", help="write a JSON result file")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if not os.path.isfile(a.vmlinux):
        die("vmlinux not found: %s" % a.vmlinux)
    try:
        tools = require_tools(a.clang_bin)
        entries = load_expected(a.expected)

        syms = parse_symbols(run([tools["llvm-nm"], a.vmlinux]))
        if not syms:
            die("llvm-nm produced no symbols - is %s a valid vmlinux?" % a.vmlinux)

        sec = find_text_section(run([tools["llvm-readelf"], "-S", "-W", a.vmlinux]))
        if sec is None:
            die("could not locate the .text section in %s" % a.vmlinux)
        text_addr, text_off, text_size = sec

        with open(a.vmlinux, "rb") as f:
            f.seek(text_off)
            text_data = f.read(text_size)
        if len(text_data) != text_size:
            die(".text is truncated: wanted %d bytes, got %d" % (text_size, len(text_data)))
    except GateError as e:
        die(str(e))

    results = []
    n_match = n_mismatch = n_na = n_missing = 0

    for kind, sym, want, lineno in entries:
        if kind == "func":
            if sym not in syms:
                if a.allow_missing:
                    results.append(dict(kind=kind, symbol=sym, expected=want,
                                        verdict="N/A", detail="symbol absent (allowed)"))
                    n_na += 1
                else:
                    results.append(dict(kind=kind, symbol=sym, expected=want,
                                        verdict="MISSING", detail="symbol absent from vmlinux"))
                    n_missing += 1
                continue
            addr, stype = syms[sym]
            rel = addr - text_addr - 4
            actual = None
            if 0 <= rel <= len(text_data) - 4:
                actual = struct.unpack_from("<I", text_data, rel)[0]
            if actual is None:
                results.append(dict(kind=kind, symbol=sym, expected=want, actual=None,
                                    verdict="MISSING",
                                    detail="symbol address 0x%X outside .text" % addr))
                n_missing += 1
                continue
            if want is None:
                verdict = "N/A"
                n_na += 1
            elif actual == want:
                verdict = "MATCH"
                n_match += 1
            else:
                verdict = "MISMATCH"
                n_mismatch += 1
            results.append(dict(kind=kind, symbol=sym, expected=want, actual=actual,
                                type=stype, addr=addr, verdict=verdict, detail=""))
        else:  # traceiter
            if sym not in syms:
                if a.allow_missing:
                    results.append(dict(kind=kind, symbol=sym, expected=want,
                                        verdict="N/A", detail="symbol absent (allowed)"))
                    n_na += 1
                else:
                    results.append(dict(kind=kind, symbol=sym, expected=want,
                                        verdict="MISSING", detail="symbol absent from vmlinux"))
                    n_missing += 1
                continue
            addr, _ = syms[sym]
            try:
                asm = run([
                    tools["llvm-objdump"], "-d",
                    "--start-address=%s" % hex(addr),
                    "--stop-address=%s" % hex(addr + 0x90),
                    a.vmlinux,
                ])
            except GateError as e:
                results.append(dict(kind=kind, symbol=sym, expected=want, actual=None,
                                    verdict="MISSING", detail="objdump failed: %s" % e))
                n_missing += 1
                continue
            actual, how = rebuild_w17_immediate(asm)
            if actual is None:
                results.append(dict(kind=kind, symbol=sym, expected=want, actual=None,
                                    verdict="MISSING", detail=how))
                n_missing += 1
                continue
            if want is None:
                verdict = "N/A"
                n_na += 1
            elif actual == want:
                verdict = "MATCH"
                n_match += 1
            else:
                verdict = "MISMATCH"
                n_mismatch += 1
            results.append(dict(kind=kind, symbol=sym, expected=want, actual=actual,
                                addr=addr, verdict=verdict, detail=how))

    # ---------------- report ----------------
    if not a.quiet:
        for kind, title in (("func", "1. Direct function preamble kCFI audit"),
                            ("traceiter", "2. Vendor hook traceiter call-site kCFI audit")):
            group = [r for r in results if r["kind"] == kind]
            if not group:
                continue
            print("=== %s ===" % title)
            for r in group:
                exp = "N/A" if r["expected"] is None else "0x%08x" % r["expected"]
                act = "N/A" if r.get("actual") is None else "0x%08x" % r["actual"]
                extra = ""
                if r["verdict"] == "MISMATCH":
                    extra = "  <-- expected 0x%08x" % r["expected"]
                elif r["detail"]:
                    extra = "  (%s)" % r["detail"]
                print("  %-56s expected=%-10s actual=%-10s %s%s"
                      % (r["symbol"], exp, act, r["verdict"], extra))
            print("")

    print("summary: %d MATCH, %d MISMATCH, %d N/A, %d MISSING (of %d entries)"
          % (n_match, n_mismatch, n_na, n_missing, len(results)))

    bad = n_mismatch + n_missing
    if bad:
        print("")
        for r in results:
            if r["verdict"] in ("MISMATCH", "MISSING"):
                exp = "N/A" if r["expected"] is None else "0x%08x" % r["expected"]
                act = "N/A" if r.get("actual") is None else "0x%08x" % r["actual"]
                print("  FAIL %-56s expected=%s actual=%s (%s)"
                      % (r["symbol"], exp, act, r["verdict"]))
        if n_mismatch:
            print("[kCFI GATE FAILED] %d type hash(es) do not match. A mismatch means "
                  "the vendor module will hit brk #0x8228 -> panic ~2-3s after boot." % n_mismatch)
        if n_missing:
            print("[kCFI GATE FAILED] %d symbol(s) could not be audited. If the kernel "
                  "legitimately dropped the hook, re-run with --allow-missing." % n_missing)
    else:
        print("[kCFI GATE PASSED] every entry is MATCH or N/A.")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(dict(vmlinux=os.path.abspath(a.vmlinux),
                           expected=os.path.abspath(a.expected),
                           match=n_match, mismatch=n_mismatch, na=n_na,
                           missing=n_missing, results=results), f, indent=2)
        if not a.quiet:
            print("json: %s" % a.json)

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateError as e:
        die(str(e))
