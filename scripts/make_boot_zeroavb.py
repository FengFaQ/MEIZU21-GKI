#!/usr/bin/env python3
"""按「已实测能开机」的参考镜像结构重打包 boot.img —— 无 AVB、无签名。

======================================================================
为什么是这个结构：来自一个已验证能开机的 GKI 范例
======================================================================
参考镜像 resukisu-boot.img（同机型、已实测能正常开机）的实测结构：

  总大小      34,369,536 B            ← 不是 96 MiB 分区大小
  header      v4 / header_size 1584 / ramdisk_size 0 / os_version 0x1c00017a
  cmdline     空 / signature_size 0
  kernel      偏移 0x1000，长度 34,363,904 B
  payload_end 0x20c7000 == 文件末尾      ← 只按 4096 对齐，不填充到分区大小
  AVB         **完全没有**：无 AVB0，EOF-64 处无 AVBf

  关键实测：参考镜像的 4096 字节头与原厂头**逐字节相同**，
  只有 kernel_size（偏移 0x0008）这 2~4 个字节不同。
  即：拿原厂头 → 只改 kernel_size → 写 kernel → 按 4096 对齐收尾。

======================================================================
为什么不用「自有密钥重签 AVB」（旧做法，已废弃）
======================================================================
AVB 的信任模型是「vbmeta 自带公钥 + 用对应私钥验自己」，但**前提是
引导链信任那把公钥**。魅族 21 原厂 vbmeta 是 RSA-4096 签名、公钥根在
魅族手里；换 kernel 后 descriptor 的 image_size/salt/digest 必然 stale，
而我们没有魅族私钥。

  保留原厂 vbmeta  → descriptor 与 kernel 不符 → 校验失败
  用自有密钥重签    → 公钥不在信任根里 → 校验失败（比不签更糟）
  **完全不写 AVB** → 引导器找不到 footer，按「无 AVB 镜像」处理 → 可启动

参考镜像走的就是第三条，并且实测能开机。本脚本与它保持一致。

======================================================================
与参考镜像的唯一有意差异：零填充到分区大小
======================================================================
参考镜像是 34,369,536 B 的裸镜像；本脚本默认输出 96 MiB
（100,663,296 B = 原厂 boot 分区大小），payload 之后全部填 0。

这样做的理由（严格优于裸镜像）：
  fastboot flash 只写镜像长度那么多字节，分区里**更后面的旧数据不会被擦掉**。
  如果该分区之前刷过带 AVB 的镜像，分区末尾 EOF-64 处会残留旧的 AVBf footer，
  引导器仍可能把它当成本次镜像的 footer。零填充到分区大小可一并清除。
  在 [0, payload_end) 区间内，输出与参考镜像的结构**完全一致**。
  需要逐字节等同参考镜像时，加 `--bare`。

用法：
  python3 make_boot_zeroavb.py <Image> <输出 boot.img> \\
      [--header-template <原厂 4096 B 头>] [--partition-size N | --bare]

退出码：0 = 结构与参考镜像一致且全部自检通过；非 0 = 失败（CI 应红掉）。
"""
import struct
import sys

PAGE = 4096
HDR_SIZE = 1584
PART_SIZE = 96 * 1024 * 1024          # 100,663,296 = 原厂 boot 分区大小
OS_VERSION_RAW = 0x1C00017A           # 14.0.0 / 2023-10

# 参考镜像实测值，用于自检时交叉印证
REF_TOTAL = 34369536
REF_KERNEL_SIZE = 34363904
REF_KERNEL_SHA256 = 'eeff5550afcc052c723d47c3143aba50cece9aca6662e6a9aba77f2e7ddadcf7'


def roundup(value: int, align: int = PAGE) -> int:
    return (value + align - 1) // align * align


def build(header_template: bytes, kernel: bytes, total_size: int | None) -> bytes:
    """克隆原厂 4096 字节头（保留全部未知字段），只改 kernel_size/ramdisk/signature。"""
    if len(header_template) < PAGE:
        raise SystemExit('[FAIL] 头模板不足 4096 字节')
    hdr_page = bytearray(header_template[:PAGE])
    if hdr_page[:8] != b'ANDROID!':
        raise SystemExit('[FAIL] 模板头不是 ANDROID!')
    if struct.unpack_from('<I', hdr_page, 40)[0] != 4:
        raise SystemExit('[FAIL] 模板头不是 header v4')

    struct.pack_into('<I', hdr_page, 8, len(kernel))     # kernel_size
    struct.pack_into('<I', hdr_page, 12, 0)              # ramdisk_size
    struct.pack_into('<I', hdr_page, 1580, 0)            # signature_size (v4)

    payload_end = PAGE + roundup(len(kernel))
    if total_size is None:                               # --bare：与参考镜像同尺寸
        total_size = payload_end
    if total_size < payload_end:
        raise SystemExit(f'[FAIL] 目标大小 {total_size} 装不下 payload {payload_end}')

    out = bytearray(total_size)                          # 全零
    out[0:HDR_SIZE] = hdr_page[0:HDR_SIZE]
    out[PAGE:PAGE + len(kernel)] = kernel
    return bytes(out)


def self_check(img: bytes, kernel: bytes, header_template: bytes,
               expect_bare: bool) -> None:
    bad = []

    def chk(label, got, want):
        ok = got == want
        if not ok:
            bad.append(f'{label}: got {got!r} want {want!r}')
        print(f'  {label:<22} {str(got):<24} {"OK" if ok else "MISMATCH"}')

    def u32(off):
        return struct.unpack_from('<I', img, off)[0]

    ks = len(kernel)
    payload_end = PAGE + roundup(ks)

    # ---- 1. header 字段（与参考镜像 / 原厂逐项一致）----
    chk('magic', img[:8], b'ANDROID!')
    chk('header_version', u32(40), 4)
    chk('header_size', u32(20), HDR_SIZE)
    chk('kernel_size', u32(8), ks)
    chk('ramdisk_size', u32(12), 0)
    chk('os_version', hex(u32(16)), hex(OS_VERSION_RAW))
    chk('cmdline', img[44:44 + 1536].split(b'\x00')[0], b'')
    chk('signature_size', u32(1580), 0)

    # ---- 2. 头只改了 kernel_size，其余字节与原厂模板逐字节相同 ----
    hdr = img[:PAGE]
    tpl = header_template[:PAGE]
    diff = [i for i in range(PAGE) if hdr[i] != tpl[i]]
    outside = [i for i in diff if not (8 <= i <= 11)]
    print(f'  {"hdr diff offsets":<22} {diff}')
    ok_hdr = not outside
    if not ok_hdr:
        bad.append(f'头里有 kernel_size 之外的改动：{outside}')
    print(f'  {"hdr only kernel_size":<22} {str(ok_hdr):<24} {"OK" if ok_hdr else "MISMATCH"}')

    # ---- 3. kernel 落位 ----
    chk('kernel bytes @0x1000', img[PAGE:PAGE + ks] == kernel, True)
    # payload_end 必须是 4096 对齐的；非 bare 模式下文件必须比它长（留出分区余量）
    aligned = payload_end % PAGE == 0
    print(f'  {"payload_end":<22} 0x{payload_end:x} ({payload_end})')
    if not aligned:
        bad.append(f'payload_end 0x{payload_end:x} 不是 4096 对齐')
    chk('payload_end aligned', aligned, True)
    if not expect_bare:
        chk('size > payload_end', len(img) > payload_end, True)

    # ---- 4. 尾部必须全零（无 AVB 残留）----
    tail = img[payload_end:]
    zero_ok = set(tail) <= {0}
    print(f'  {"tail_zero_filled":<22} {str(zero_ok):<24} {"OK" if zero_ok else "MISMATCH"}')
    if not zero_ok:
        bad.append('尾部有非零数据（AVB 残留？）')
    if expect_bare:
        chk('size == payload_end', len(img), payload_end)

    # ---- 5. 绝对不能有 AVB 痕迹 ----
    avb0 = img.find(b'AVB0')
    chk('no AVB0 magic', avb0 == -1, True)
    if avb0 != -1:
        bad.append(f'发现 AVB0 @0x{avb0:x}')
    # 标签写成「有/无」的正向断言，避免 got=False 被误读成失败
    chk('has AVBf @EOF-64', img[-64:-60] == b'AVBf', False)
    avbf_any = img.find(b'AVBf')
    chk('has AVBf anywhere', avbf_any != -1, False)
    if avbf_any != -1:
        bad.append(f'发现 AVBf @0x{avbf_any:x}')

    if bad:
        print('BOOT IMG CHECK FAILED:')
        for b in bad:
            print('  - ' + b)
        raise SystemExit(1)
    print('BOOT IMG CHECK PASSED（与可开机参考镜像同构：原厂头 + kernel + 零填充，无 AVB）')


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    image_path, out_path = sys.argv[1], sys.argv[2]

    def opt(name):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None

    bare = '--bare' in sys.argv
    part_size = None if bare else PART_SIZE
    ps = opt('--partition-size')
    if ps:
        part_size = int(ps, 0)
        bare = False
    hdr_tpl_path = opt('--header-template')

    kernel = open(image_path, 'rb').read()
    if not kernel:
        raise SystemExit('[FAIL] Image 为空')
    if len(kernel) >= 94371840:
        raise SystemExit(f'[FAIL] Image 太大（{len(kernel)} B > 90 MiB）')

    # arm64 Image 头：偏移 0x38 处是稳定魔数 "ARM\x64"（即 ARMd）。
    # 不要用前 8 字节——第 4~7 字节是 `b` 跳转指令的相对偏移，随内核大小变化。
    if kernel[0x38:0x3c] != b'ARM\x64':
        print(f'[warn] Image 头在 0x38 处不是 ARMd 魔数：{kernel[0x38:0x3c]!r}')

    if hdr_tpl_path:
        tpl = open(hdr_tpl_path, 'rb').read()
    else:
        # 退化路径：手工构造 v4 头（无原厂模板时；会丢掉原厂未知字段）
        print('[warn] 未给 --header-template，用手工构造的 v4 头')
        tpl = bytearray(PAGE)
        tpl[0:8] = b'ANDROID!'
        struct.pack_into('<I', tpl, 8, len(kernel))
        struct.pack_into('<I', tpl, 12, 0)
        struct.pack_into('<I', tpl, 16, OS_VERSION_RAW)
        struct.pack_into('<I', tpl, 20, HDR_SIZE)
        struct.pack_into('<I', tpl, 40, 4)
        tpl = bytes(tpl)

    img = build(tpl, kernel, part_size)
    open(out_path, 'wb').write(img)
    print(f'[OK] {out_path} ({len(img):,} B), kernel={len(kernel):,} B')
    self_check(img, kernel, tpl, expect_bare=bare)
    return 0


if __name__ == '__main__':
    sys.exit(main())
