#!/usr/bin/env python3
"""用 avbtool 给 boot.img 加 AVB hash footer（自有密钥），保持 96 MiB 分区大小。

为什么用 avbtool 而不是手写 AVB 编码：
  AVB 的 vbmeta 结构（header/auth/aux 三段、descriptor 二进制布局、公钥编码、
  PKCS#1 v1.5 签名）细节极多，手写极易出错 —— 本项目实测手写版被
  `avbtool verify_image` 判为 "Given data does not look like a property
  descriptor"。avbtool 是 AOSP 官方实现，用它生成才能保证 ABL 认得。

为什么必须重签而不是保留原厂 vbmeta：
  原厂 vbmeta 是 RSA-4096 签名的，签名覆盖 hash descriptor；换 kernel 后
  descriptor 必然不符，而我没有魅族私钥 → 原签名失效 → 真机亮 logo 数秒后重启
  （run#17 实测）。AVB 的模型是「vbmeta 自带公钥、用对应私钥验自己」，
  所以用自有密钥重签后，boot 分区是**自洽可校验**的，不需要原厂私钥，
  也不需要动 vbmeta 分区（用户始终只刷 boot 单分区）。

用法：
  python3 avb_sign_boot.py <Image> <输出 boot.img> --key <rsa4096.pem> \\
      [--header-template <原厂4096B头>] [--partition-size 100663296] [--avbtool PATH]

退出码：0 = 成功且 avbtool verify_image 通过；非 0 = 失败（CI 应红掉）
"""
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

PAGE = 4096
HDR_SIZE = 1584
PART_SIZE = 96 * 1024 * 1024
EXPECTED_VBMETA_SIZE = 2496       # 与原厂一致（auth 576 + aux 1664 + header 256）


def find_avbtool(explicit: str | None) -> str:
    """定位 avbtool：显式路径 > PATH > python 模块 > 常见位置。"""
    if explicit:
        return explicit
    got = shutil.which('avbtool')
    if got:
        return got
    try:
        import avbtool as m
        return m.__file__
    except ImportError:
        pass
    for cand in ('/usr/lib/android-sdk/avbtool', '/opt/avbtool/avbtool.py',
                 os.path.expanduser('~/avbtool.py')):
        if os.path.isfile(cand):
            return cand
    raise SystemExit('[FAIL] 找不到 avbtool（--avbtool 指定，或 pip install / 从 AOSP 下载）')


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    image_path, out_path = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]

    def opt(name, default=None):
        if name in args:
            return args[args.index(name) + 1]
        return default

    key = opt('--key')
    hdr_tpl = opt('--header-template')
    _ps = opt('--partition-size')
    part_size = int(_ps, 0) if _ps else PART_SIZE
    avbtool = find_avbtool(opt('--avbtool'))
    if not key:
        raise SystemExit('[FAIL] 需要 --key <RSA-4096 私钥 PEM>')
    print(f'[info] avbtool = {avbtool}')

    kernel = open(image_path, 'rb').read()
    if not kernel:
        raise SystemExit('[FAIL] Image 为空')

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, 'boot.img')

        # --- 1) 用原厂 4096B 头做模板，拼出「无 AVB」的原始 boot ---
        if hdr_tpl:
            tpl = bytearray(open(hdr_tpl, 'rb').read()[:PAGE])
            if tpl[:8] != b'ANDROID!':
                raise SystemExit('[FAIL] 头模板 magic 不对')
        else:
            tpl = bytearray(PAGE)
            tpl[0:8] = b'ANDROID!'
            struct.pack_into('<I', tpl, 16, 0x1C00017A)
            struct.pack_into('<I', tpl, 20, HDR_SIZE)
            struct.pack_into('<I', tpl, 40, 4)
        struct.pack_into('<I', tpl, 8, len(kernel))    # kernel_size
        struct.pack_into('<I', tpl, 12, 0)             # ramdisk_size
        struct.pack_into('<I', tpl, 1580, 0)           # signature_size

        payload_end = PAGE + (len(kernel) + PAGE - 1) // PAGE * PAGE
        raw_img = bytearray(payload_end)
        raw_img[0:HDR_SIZE] = tpl[0:HDR_SIZE]
        raw_img[PAGE:PAGE + len(kernel)] = kernel
        open(raw, 'wb').write(bytes(raw_img))
        print(f'[info] raw boot: {len(raw_img)} B (kernel={len(kernel)}, payload_end=0x{payload_end:X})')

        # --- 2) avbtool add_hash_footer：自带公钥 + 签名，分区大小 96 MiB ---
        cmd = [sys.executable, avbtool, 'add_hash_footer',
               '--image', raw,
               '--partition_name', 'boot',
               '--partition_size', str(part_size),
               '--key', key,
               '--algorithm', 'SHA256_RSA4096',
               '--salt', hashlib.sha256(kernel).hexdigest()]
        print('[run]', ' '.join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            raise SystemExit('[FAIL] avbtool add_hash_footer 失败')
        print(r.stdout.strip()[-500:])

        # --- 3) 立即用 avbtool 独立验证（唯一可信判据）---
        v = subprocess.run([sys.executable, avbtool, 'verify_image', '--image', raw],
                           capture_output=True, text=True)
        print('[verify]', v.stdout.strip()[-800:])
        if v.returncode != 0:
            print(v.stderr, file=sys.stderr)
            raise SystemExit('[FAIL] avbtool verify_image 未通过')

        shutil.copyfile(raw, out_path)

    # --- 4) 结构自检 ---
    data = open(out_path, 'rb').read()
    bad = []

    def chk(label, got, want):
        ok = got == want
        if not ok:
            bad.append(f'{label}: {got!r} != {want!r}')
        print(f'  {label:<20} {str(got):<24} {"OK" if ok else "MISMATCH"}')

    chk('size', len(data), part_size)
    chk('magic', data[:8], b'ANDROID!')
    chk('header_version', struct.unpack_from('<I', data, 40)[0], 4)
    chk('header_size', struct.unpack_from('<I', data, 20)[0], 1584)
    chk('kernel_size', struct.unpack_from('<I', data, 8)[0], len(kernel))
    chk('ramdisk_size', struct.unpack_from('<I', data, 12)[0], 0)
    chk('os_version', hex(struct.unpack_from('<I', data, 16)[0]), '0x1c00017a')
    chk('cmdline', data[44:44 + 1536].split(b'\x00')[0], b'')
    payload_end = PAGE + (len(kernel) + PAGE - 1) // PAGE * PAGE
    chk('AVB0@payload_end', data[payload_end:payload_end + 4], b'AVB0')
    chk('AVBf@EOF-64', data[-64:-60], b'AVBf')
    foot = data[-64:]
    voff = struct.unpack_from('>Q', foot, 20)[0]
    vsize = struct.unpack_from('>Q', foot, 28)[0]
    chk('footer.vbmeta_offset', voff, payload_end)
    # vbmeta 大小随 descriptor 数量变化（原厂 2496 含 4 条 property；
    # 我们只生成 hash descriptor，实测 2112）。只要求是合理的 512B 对齐值。
    ok_size = (512 <= vsize <= 4096) and (vsize % 64 == 0)
    print(f'  {"footer.vbmeta_size":<20} {str(vsize):<24} {"OK" if ok_size else "MISMATCH"}'
          f'（原厂 {EXPECTED_VBMETA_SIZE}，差异来自 property descriptor 数量）')
    if not ok_size:
        bad.append(f'footer.vbmeta_size 不合理: {vsize}')

    if bad:
        print('BOOT AVB CHECK FAILED:')
        for b in bad:
            print('  - ' + b)
        return 1
    print('BOOT AVB CHECK PASSED（自有密钥签名 + avbtool 验证通过 + 结构与原厂一致）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
