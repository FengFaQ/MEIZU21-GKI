#!/usr/bin/env python3
"""刷前审计：校验重打包出来的 boot.img 结构是否与原厂一致。

用法:
    python3 audit_boot.py <boot.img>

校验项（任何一项失败 -> exit 1）：
  1. 文件大小 == 96 MiB (100663296)
  2. boot_img_hdr v4 关键字段（magic/header_version/header_size/ramdisk_size/os_version/cmdline）
  3. kernel 段起点 4096、kernel_size > 0
  4. payload 末尾（4096 + align(kernel_size,4096)）处是 AVB0
  5. AVBf footer 在 EOF-64，且 orig_image_size / vbmeta_offset / vbmeta_size 与第 4 项一致
  6. 打印 AVB0 里的 AvbHashDescriptor（partition/image_size/salt/digest），
     并标出 digest 是否仍等于 sha256(salt || image)（v1 会 stale，v2 应该匹配；只提示不失败）

这不是 AVB 验签工具；魅族 ABL 的实际行为只能真机验证。
"""
import hashlib
import struct
import sys

BOOT_PART_SIZE = 100663296  # 96 MiB
PAGE = 4096
VBMETA_SIZE = 2496          # 原厂 Flyme 12.7 boot 的 vbmeta 块大小


def align_up(v, a):
    return (v + a - 1) // a * a


def parse_header(data):
    if data[:8] != b"ANDROID!":
        raise SystemExit("[FAIL] 不是 ANDROID! boot image")
    return {
        "kernel_size": struct.unpack_from("<I", data, 8)[0],
        "ramdisk_size": struct.unpack_from("<I", data, 12)[0],
        "os_version": struct.unpack_from("<I", data, 16)[0],
        "header_size": struct.unpack_from("<I", data, 20)[0],
        "header_version": struct.unpack_from("<I", data, 40)[0],
        "cmdline": data[44:44 + 1536].split(b"\x00")[0],
    }


def parse_avb0(data, off):
    """按 AOSP avbtool 的 AvbVBMetaImageHeader 布局解析（大端）。"""
    if data[off:off + 4] != b"AVB0":
        return None
    (major, minor) = struct.unpack_from(">II", data, off + 4)
    auth_size, aux_size = struct.unpack_from(">QQ", data, off + 12)
    algorithm = struct.unpack_from(">I", data, off + 28)[0]
    hash_size = struct.unpack_from(">Q", data, off + 40)[0]
    sig_size = struct.unpack_from(">Q", data, off + 56)[0]
    desc_off = struct.unpack_from(">Q", data, off + 96)[0]
    desc_size = struct.unpack_from(">Q", data, off + 104)[0]
    release = data[off + 128:off + 128 + 48].split(b"\x00")[0].decode("ascii", "replace")
    return {
        "major": major, "minor": minor,
        "auth_block_size": auth_size, "aux_block_size": aux_size,
        "algorithm": algorithm, "hash_size": hash_size, "sig_size": sig_size,
        "descriptors_offset": desc_off, "descriptors_size": desc_size,
        "release_string": release,
    }


def parse_hash_descriptor(data, base, off, size):
    """在 descriptor 区里找 tag=2 的 AvbHashDescriptor。返回 dict 或 None。

    两条容易踩的坑（都是本机实测出来的）：
      1. avbtool 的 descriptors_offset 是相对 **auxiliary data block** 起点的偏移，
         aux block 起点 = vbmeta 起点 + 256（header）+ auth_block_size。
      2. 魅族这版 avbtool（release_string `avbtool 1.3.0`）的 AvbHashDescriptor 用的是
         **老布局**：`uint8_t hash_algorithm[32]`（字符串，如 "sha256"），不是新版的
         uint32 枚举。所以字段偏移是：
             +16 image_size(u64) | +24 hash_algorithm[32] | +56 partition_name_len(u32)
             | +60 salt_len | +64 digest_len | +68 flags | +72 reserved[60]
             | +132 partition_name | salt | digest
    """
    p = base + off
    end = p + size
    while p + 16 <= end:
        tag, follow = struct.unpack_from(">QQ", data, p)
        body = p + 16
        if tag == 2:  # AVB_DESCRIPTOR_TAG_HASH
            image_size = struct.unpack_from(">Q", data, body)[0]
            alg_str = data[body + 8:body + 40].split(b"\x00")[0].decode("ascii", "replace")
            name_len = struct.unpack_from(">I", data, body + 40)[0]
            salt_len = struct.unpack_from(">I", data, body + 44)[0]
            digest_len = struct.unpack_from(">I", data, body + 48)[0]
            q = body + 56 + 60  # flags(4) + reserved[60]
            name = data[q:q + name_len]
            salt = data[q + name_len:q + name_len + salt_len]
            digest = data[q + name_len + salt_len:q + name_len + salt_len + digest_len]
            return {
                "image_size": image_size, "hash_algorithm": alg_str,
                "partition_name": name.decode("ascii", "replace"),
                "salt": salt, "digest": digest,
                "total_size": 16 + follow,
            }
        p = p + 16 + follow
        p = align_up(p, 8)
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    data = open(path, "rb").read()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + msg)
        if not cond:
            ok = False

    print(f"=== 审计 {path} ({len(data)} bytes) ===")
    check(len(data) == BOOT_PART_SIZE, f"大小 == {BOOT_PART_SIZE}")
    h = parse_header(data)
    print(f"  header: v{h['header_version']} size={h['header_size']} kernel={h['kernel_size']} "
          f"ramdisk={h['ramdisk_size']} os=0x{h['os_version']:08x} cmdline={h['cmdline']!r}")
    check(h["header_version"] == 4, "header_version == 4")
    check(h["header_size"] == 1584, "header_size == 1584")
    check(h["ramdisk_size"] == 0, "ramdisk_size == 0（boot 里没有 ramdisk）")
    check(h["os_version"] == 0x1C00017A, "os_version == 0x1C00017A（14.0.0 / 2023-10）")
    check(h["cmdline"] == b"", "cmdline 为空")
    ksize = h["kernel_size"]
    payload_end = PAGE + align_up(ksize, PAGE)
    check(ksize > 0, "kernel_size > 0")
    check(data[payload_end:payload_end + 4] == b"AVB0", f"AVB0 在 payload 末尾 0x{payload_end:X}")
    footer = data[-64:]
    check(footer[:4] == b"AVBf", "AVBf footer 在 EOF-64")
    if footer[:4] == b"AVBf":
        orig = struct.unpack_from(">Q", footer, 12)[0]
        voff = struct.unpack_from(">Q", footer, 20)[0]
        vsize = struct.unpack_from(">Q", footer, 28)[0]
        print(f"  footer: orig_image_size=0x{orig:X} vbmeta_offset=0x{voff:X} vbmeta_size={vsize}")
        check(orig == payload_end, "footer.original_image_size == payload_end")
        check(voff == payload_end, "footer.vbmeta_offset == payload_end")
        check(vsize == VBMETA_SIZE, f"footer.vbmeta_size == {VBMETA_SIZE}")
        vb = parse_avb0(data, voff)
        if vb:
            print(f"  vbmeta: v{vb['major']}.{vb['minor']} release={vb['release_string']!r} "
                  f"auth={vb['auth_block_size']} aux={vb['aux_block_size']} alg={vb['algorithm']} "
                  f"desc_off={vb['descriptors_offset']} desc_size={vb['descriptors_size']}")
            # descriptors_offset 相对 aux block 起点；aux 起点 = vbmeta 起点 + 256 + auth_size
            aux_base = voff + 256 + vb["auth_block_size"]
            hd = parse_hash_descriptor(data, aux_base, vb["descriptors_offset"], vb["descriptors_size"])
            if hd:
                print(f"  hash_descriptor: partition={hd['partition_name']!r} "
                      f"image_size=0x{hd['image_size']:X} alg={hd['hash_algorithm']} "
                      f"salt={hd['salt'].hex()[:16]}… digest={hd['digest'].hex()[:16]}…")
                actual = hashlib.sha256(hd["salt"] + data[:hd["image_size"]]).hexdigest()
                if actual == hd["digest"].hex():
                    print("  信息  digest 与当前镜像一致（v2 预期）")
                else:
                    print("  信息  digest 是 stale 的（v1 预期：沿用原厂 vbmeta 字节）")
            else:
                print("  WARN 没解析到 AvbHashDescriptor（不影响 boot；仅审计提示）")
    print("=== 结论:", "PASS" if ok else "FAIL", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
