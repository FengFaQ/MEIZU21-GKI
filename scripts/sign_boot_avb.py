#!/usr/bin/env python3
"""用自有密钥重签 boot.img 的 AVB hash footer —— 让分区内 vbmeta 自洽。

背景（为什么必须这样做）：
  魅族 21 原厂 boot.img 的 vbmeta 带 **RSA-4096 签名**，签名覆盖 hash descriptor。
  我们换了 kernel ⇒ descriptor 里的 image_size/salt/digest 与内容不符 ⇒ 原厂签名
  必然失效。而没有魅族私钥就不可能让「原厂签名 + 新内容」同时成立。
  实测 run#17（保留原厂 vbmeta 字节）真机「亮 logo 数秒后重启」。

  AVB 的校验模型是：**vbmeta 内自带 public key，用对应的 private key 签自己**。
  所以正确做法是：用我们自己的密钥重新生成整个 vbmeta（含我们的 pubkey），
  并让 hash descriptor 描述真实内容 —— 这样 boot 分区自身就是自洽可校验的，
  不需要原厂私钥，也不需要动 vbmeta 分区（用户只刷 boot 单分区）。

  已核对原厂结构：avbtool 1.3.0 风格，auth_block=576（hash 32 + sig 512 + 32），
  aux_block=1664（desc 608 + pubkey 1032 + 24 余量），descriptors_offset=0
  （相对 aux 起点），AvbHashDescriptor 用老布局（hash_algorithm 是 32 字节字符串）。

本脚本不依赖 avbtool 二进制，纯 Python 实现（CI 里没有 avbtool 时也能跑）。

用法：
  python3 sign_boot_avb.py <Image> <输出 boot.img> \\
      --key <pkcs8|pem 私钥> --header-template <原厂4096B头> [--partition-size N]
"""
import hashlib
import struct
import sys

PAGE = 4096
HDR_SIZE = 1584
PART_SIZE = 96 * 1024 * 1024
AVB_MAGIC = b'AVB0'
AVBF_MAGIC = b'AVBf'
ALGORITHM_SHA256_RSA4096 = 2
HASH_SIZE = 32
SIG_SIZE = 512
PUBKEY_SIZE = 1032          # AVB RSA-4096 pubkey 编码长度（4+4+512+512）
DESC_NAME = b'boot'


# ---------------------------------------------------------------- AVB 编码
def encode_avb_pubkey(rsa_n: int, rsa_e: int = 65537) -> bytes:
    """AVB 公钥编码：key_num_bits(4) n0inv(4) modulus(512, BE) rr(512, BE)。"""
    nbits = rsa_n.bit_length()
    if nbits != 4096:
        raise SystemExit(f'[FAIL] 需要 RSA-4096 密钥，当前 {nbits} bit')
    # n0inv = -1/n mod 2^32
    n0inv = (-pow(rsa_n, -1, 1 << 32)) & 0xFFFFFFFF
    # rr = 2^(2*nbits) mod n
    rr = pow(2, 2 * nbits, rsa_n)
    return (struct.pack('>II', nbits, n0inv)
            + rsa_n.to_bytes(512, 'big')
            + rr.to_bytes(512, 'big'))


def pkcs1_v15_sign(priv, digest: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5（DigestInfo 前缀 SHA-256）。"""
    di = bytes.fromhex('3031300d060960864801650304020105000420') + digest
    n = priv.n
    k = 512
    eb = b'\x00\x01' + b'\xff' * (k - len(di) - 3) + b'\x00' + di
    m = int.from_bytes(eb, 'big')
    s = pow(m, priv.d, n)
    return s.to_bytes(k, 'big')


def build_hash_descriptor(image_size: int, salt: bytes, digest: bytes) -> bytes:
    """老布局 AvbHashDescriptor（与魅族原厂一致）。
    tag(8) num(8) image_size(8) hash_algorithm[32] name_len(4) salt_len(4)
    digest_len(4) flags(4) reserved[60] name salt digest
    """
    body = struct.pack('>Q', image_size)
    body += b'sha256'.ljust(32, b'\x00')
    body += struct.pack('>III', len(DESC_NAME), len(salt), len(digest))
    body += struct.pack('>I', 0) + b'\x00' * 60
    body += DESC_NAME + salt + digest
    # 8 字节对齐
    if len(body) % 8:
        body += b'\x00' * (8 - len(body) % 8)
    return struct.pack('>QQ', 2, len(body)) + body


def build_vbmeta(image_prefix: bytes, salt: bytes, priv, pubkey_bytes: bytes) -> bytes:
    """构造完整 vbmeta：header(256) + auth(hash+sig) + aux(desc+pubkey+pad)。"""
    digest = hashlib.sha256(image_prefix + salt).digest()
    salt = salt  # descriptor 里记录 salt

    desc = build_hash_descriptor(len(image_prefix), salt, digest)
    aux = desc + pubkey_bytes
    aux = aux.ljust(1664, b'\x00')
    desc_off = 0

    # header 先占位，算完 hash 再填签名
    header = bytearray(256)
    header[0:4] = AVB_MAGIC
    struct.pack_into('>I', header, 4, 1)          # required major
    struct.pack_into('>I', header, 8, 0)          # required minor
    struct.pack_into('>Q', header, 12, 576)       # auth_block_size
    struct.pack_into('>Q', header, 20, len(aux))  # aux_block_size
    struct.pack_into('>I', header, 28, ALGORITHM_SHA256_RSA4096)
    struct.pack_into('>Q', header, 32, 0)         # hash_offset
    struct.pack_into('>Q', header, 40, HASH_SIZE)  # hash_size
    struct.pack_into('>Q', header, 48, HASH_SIZE)  # signature_offset
    struct.pack_into('>Q', header, 56, SIG_SIZE)   # signature_size
    struct.pack_into('>Q', header, 64, len(desc))  # public_key_offset
    struct.pack_into('>Q', header, 72, len(pubkey_bytes))
    struct.pack_into('>Q', header, 80, len(desc) + len(pubkey_bytes))
    struct.pack_into('>Q', header, 88, 0)
    struct.pack_into('>Q', header, 96, desc_off)   # descriptors_offset（相对 aux）
    struct.pack_into('>Q', header, 104, len(desc))
    struct.pack_into('>Q', header, 112, 0)         # rollback_index
    struct.pack_into('>I', header, 120, 0)         # flags
    struct.pack_into('>I', header, 124, 0)         # rollback_index_location
    header[128:128 + 12] = b'avbtool 1.3.0'

    # auth block = hash(32) + signature(512) + padding(32)
    vbmeta_unsigned = bytes(header) + bytes(576) + aux
    vb_hash = hashlib.sha256(vbmeta_unsigned).digest()
    sig = pkcs1_v15_sign(priv, vb_hash)
    auth = vb_hash + sig
    auth = auth.ljust(576, b'\x00')
    return bytes(header) + auth + aux


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

    key_path = opt('--key')
    hdr_tpl = opt('--header-template')
    _ps = opt('--partition-size')
    part_size = int(_ps, 0) if _ps else PART_SIZE
    if not key_path:
        raise SystemExit('[FAIL] 需要 --key <私钥>')

    # --- 读私钥（支持 PEM PKCS#1/PKCS#8）---
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise SystemExit('[FAIL] 需要 python3-cryptography（pip install cryptography）')
    pem = open(key_path, 'rb').read()
    priv = serialization.load_pem_private_key(pem, password=None)
    nums = priv.private_numbers()
    rsa_n, rsa_d = nums.public_numbers.n, nums.d

    kernel = open(image_path, 'rb').read()
    if not kernel:
        raise SystemExit('[FAIL] Image 为空')

    # --- 头部：克隆原厂 4096B 头，只改 kernel_size/ramdisk/signature ---
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
    struct.pack_into('<I', tpl, 8, len(kernel))
    struct.pack_into('<I', tpl, 12, 0)
    struct.pack_into('<I', tpl, 1580, 0)

    # --- 布局：header | kernel | pad | vbmeta | pad | AVBf ---
    payload_end = PAGE + (len(kernel) + PAGE - 1) // PAGE * PAGE
    salt = hashlib.sha256(kernel).digest()
    image_prefix = bytes(tpl[:HDR_SIZE]) + bytes(payload_end - HDR_SIZE)
    image_prefix = bytearray(image_prefix)
    image_prefix[PAGE:PAGE + len(kernel)] = kernel
    image_prefix = bytes(image_prefix)

    pubkey_bytes = encode_avb_pubkey(rsa_n)
    vbmeta = build_vbmeta(image_prefix, salt, type('P', (), {'n': rsa_n, 'd': rsa_d})(), pubkey_bytes)

    out = bytearray(part_size)
    out[0:len(image_prefix)] = image_prefix
    out[payload_end:payload_end + len(vbmeta)] = vbmeta
    # AVBf footer
    avbf = bytearray(64)
    avbf[0:4] = AVBF_MAGIC
    struct.pack_into('>I', avbf, 4, 1)
    struct.pack_into('>I', avbf, 8, 0)
    struct.pack_into('>Q', avbf, 12, payload_end)   # original_image_size
    struct.pack_into('>Q', avbf, 20, payload_end)   # vbmeta_offset
    struct.pack_into('>Q', avbf, 28, len(vbmeta))   # vbmeta_size
    out[part_size - 64:part_size] = avbf

    open(out_path, 'wb').write(bytes(out))
    print(f'[OK] {out_path} ({part_size} B), kernel={len(kernel)}, '
          f'payload_end=0x{payload_end:X}, vbmeta={len(vbmeta)}')
    print(f'[OK] 已用自有密钥重签（RSA-{rsa_n.bit_length()}），分区内自洽')
    return 0


if __name__ == '__main__':
    sys.exit(main())
