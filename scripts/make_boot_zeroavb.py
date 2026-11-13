#!/usr/bin/env python3
"""按「能开机的参考结构」重打包 boot.img：mkbootimg 结构 + 零填充，不含 AVB。

为什么不用克隆原厂 AVB 的骨架法（旧做法）：
  魅族 21 原厂 boot.img 的 vbmeta 是 RSA-4096 **签名**的，签名覆盖 hash descriptor。
  换了 kernel 之后 descriptor 里的 image_size/salt/digest 必然 stale，而我们没有
  魅族私钥无法重签 → ABL 校验失败 → 亮 logo 数秒后自动重启（实测 run#17 现象）。
  参考能开机的 MEIZU21GKI 项目：它走「不签名 + 零填充到分区大小」，
  自检断言尾部全 0（tail zero-filled True），设备正常启动。

本脚本产出：
  - 96 MiB（= 原厂 boot 分区大小 100663296）的 boot.img
  - header v4 / header_size 1584 / ramdisk 0 / os_version 0x1c00017a / cmdline 空
  - payload 之后**全部零填充**，没有任何 AVB0 / AVBf 残留
  - 结构自检与原厂逐字段一致（除 AVB 部分），不一致直接失败

用法：python3 make_boot_zeroavb.py <Image> <输出 boot.img> [--partition-size N]
"""
import struct
import sys

PAGE = 4096
HDR_SIZE = 1584
PART_SIZE = 96 * 1024 * 1024          # 100663296，原厂 boot 分区大小
OS_VERSION_RAW = 0x1C00017A           # 14.0.0 / 2023-10


def build(header_template: bytes, kernel: bytes) -> bytes:
    """克隆原厂 4096 字节头（保留全部字段），只改 kernel_size / ramdisk / signature。"""
    hdr_page = bytearray(header_template[:PAGE])
    if hdr_page[:8] != b'ANDROID!':
        raise SystemExit('[FAIL] 模板头不是 ANDROID!')
    struct.pack_into('<I', hdr_page, 8, len(kernel))     # kernel_size
    struct.pack_into('<I', hdr_page, 12, 0)              # ramdisk_size
    struct.pack_into('<I', hdr_page, 1580, 0)            # signature_size (v4)

    out = bytearray(PART_SIZE)                           # 全零
    out[0:HDR_SIZE] = hdr_page[0:HDR_SIZE]
    out[PAGE:PAGE + len(kernel)] = kernel
    return bytes(out)


def self_check(img: bytes, kernel_size: int) -> None:
    bad = []

    def chk(label, got, want):
        ok = got == want
        if not ok:
            bad.append(f'{label}: got {got!r} want {want!r}')
        print(f'  {label:<18} {got!r:<26} {"OK" if ok else "MISMATCH"}')

    def u32(off):
        return struct.unpack_from('<I', img, off)[0]

    chk('size', len(img), PART_SIZE)
    chk('magic', img[:8], b'ANDROID!')
    chk('header_version', u32(40), 4)
    chk('header_size', u32(20), 1584)
    chk('kernel_size', u32(8), kernel_size)
    chk('ramdisk_size', u32(12), 0)
    chk('os_version', hex(u32(16)), hex(OS_VERSION_RAW))
    chk('cmdline', img[44:44 + 1536].split(b'\x00')[0], b'')
    chk('signature_size', u32(1580), 0)

    payload_end = PAGE + (kernel_size + PAGE - 1) // PAGE * PAGE
    tail = img[payload_end:]
    zero_ok = tail.count(0) == len(tail)
    print(f'  {"tail_zero_filled":<18} {str(zero_ok):<26} {"OK" if zero_ok else "MISMATCH"}')
    if not zero_ok:
        bad.append('尾部有非零数据（AVB 残留？）')

    # 关键：不能有任何 AVB 痕迹
    has_avb0 = img.find(b'AVB0') != -1
    has_avbf = img[-64:-60] == b'AVBf'
    chk('no AVB0 magic', has_avb0, False)
    chk('no AVBf footer', has_avbf, False)

    if bad:
        print('BOOT IMG CHECK FAILED:')
        for b in bad:
            print('  - ' + b)
        raise SystemExit(1)
    print('BOOT IMG CHECK PASSED（零填充结构，无 AVB，与可启动参考镜像一致）')


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    image_path, out_path = sys.argv[1], sys.argv[2]
    part_size = PART_SIZE
    if '--partition-size' in sys.argv:
        part_size = int(sys.argv[sys.argv.index('--partition-size') + 1], 0)
    # 允许用 devicetree 里保存的原厂头做模板（保留所有未知字段）
    hdr_tpl_path = None
    if '--header-template' in sys.argv:
        hdr_tpl_path = sys.argv[sys.argv.index('--header-template') + 1]

    kernel = open(image_path, 'rb').read()
    if not kernel:
        raise SystemExit('[FAIL] Image 为空')

    if hdr_tpl_path:
        tpl = open(hdr_tpl_path, 'rb').read()
    else:
        # 退化路径：手工构造 v4 头（无模板时）
        tpl = bytearray(PAGE)
        tpl[0:8] = b'ANDROID!'
        struct.pack_into('<I', tpl, 8, len(kernel))
        struct.pack_into('<I', tpl, 12, 0)
        struct.pack_into('<I', tpl, 16, OS_VERSION_RAW)
        struct.pack_into('<I', tpl, 20, HDR_SIZE)
        struct.pack_into('<I', tpl, 28, OS_VERSION_RAW)
        struct.pack_into('<I', tpl, 40, 4)       # header_version
        tpl = bytes(tpl)

    globals()['PART_SIZE'] = part_size
    img = build(tpl, kernel)
    # build() 里用了模块级 PART_SIZE，这里按参数重截断
    if len(img) != part_size:
        img = img[:part_size] if len(img) > part_size else img + bytes(part_size - len(img))

    open(out_path, 'wb').write(img)
    print(f'[OK] {out_path} ({len(img)} bytes = {len(img)//1024//1024} MiB), kernel={len(kernel)}')
    self_check(img, len(kernel))
    return 0


if __name__ == '__main__':
    sys.exit(main())
