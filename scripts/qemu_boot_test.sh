#!/usr/bin/env bash
# QEMU 早期启动验证：把 boot.img 里的内核丢进 QEMU virt 跑一遍，检查是否崩在早期。
#
# 能证明：boot.img 结构正确、内核可被加载跳转、无 kCFI trap / Oops / Unknown symbol
#          （即「内核二进制健康」）。
# 不能证明：真机能开机（QEMU virt 无 SM8650 硬件、无 vendor 模块、不校验 AVB）。
#
# 零退出码 = 通过；非零 = 内核在早期崩溃（附错误摘要）。
#
# 用法：bash scripts/qemu_boot_test.sh <boot.img> [--expect-stock]
set -uo pipefail

BOOT="${1:?用法: qemu_boot_test.sh <boot.img>}"
WORK="${WORK:-$(mktemp -d)}"
mkdir -p "$WORK"

if ! command -v qemu-system-aarch64 >/dev/null 2>&1; then
  echo "[skip] 未安装 qemu-system-aarch64"
  exit 0
fi

IMG="$WORK/Image"
python3 - "$BOOT" "$IMG" <<'PY'
import struct, sys
boot, out = sys.argv[1], sys.argv[2]
d = open(boot, 'rb').read()
assert d[:8] == b'ANDROID!', '不是 Android boot.img'
ks = struct.unpack_from('<I', d, 8)[0]
body = d[4096:4096 + ks]
assert len(body) == ks, f'kernel 段长度不符: {len(body)} != {ks}'
assert body[0x38:0x3c] == b'ARMd', 'kernel 段没有 ARM64 Image 魔数（压缩过？）'
open(out, 'wb').write(body)
print(f'[qemu] 提取内核 {ks} 字节（ARMd 魔数 OK）')
PY
[ -s "$IMG" ] || { echo "::error::内核提取失败"; exit 1; }

echo "[qemu] 启动中（60s 超时，无 KVM 软件模拟）…"
timeout 60 qemu-system-aarch64 \
  -machine virt,gic-version=3 -cpu cortex-a710 -smp 2 -m 2048 \
  -kernel "$IMG" -nographic -no-reboot \
  -append "console=ttyAMA0 earlycon=pl011,0x9000000 keep_bootcon rdinit=/bin/sh" \
  > "$WORK/console.log" 2>&1
rc=$?
lines=$(wc -l < "$WORK/console.log")
echo "[qemu] exit=$rc, 日志 $lines 行, 保存到 $WORK/console.log"

# --- 结构性失败：内核根本没跑起来 ---
if [ "$lines" -lt 30 ]; then
  echo "::error::QEMU 日志过短（$lines 行），内核疑似未被加载"
  tail -20 "$WORK/console.log"
  exit 1
fi
if ! grep -q 'Booting Linux on physical CPU' "$WORK/console.log"; then
  echo "::error::没有出现 'Booting Linux on physical CPU'，内核未进入 start_kernel"
  tail -20 "$WORK/console.log"
  exit 1
fi

# --- 早期崩溃检测（这些是真问题）---
fail=0
check_absent() {
  local pat="$1" why="$2"
  if grep -qiE "$pat" "$WORK/console.log"; then
    echo "::error::检测到 $why"
    grep -iE "$pat" "$WORK/console.log" | head -5
    fail=1
  else
    echo "  [OK] 无 $why"
  fi
}
echo "[qemu] 早期崩溃检测："
check_absent 'CFI failure|expected type 0x'   'kCFI trap'
check_absent 'Unknown symbol'                  '未导出符号'
check_absent 'disagrees about version'         '符号版本不符'
check_absent 'Unable to handle kernel'         '内核内存访问异常'
check_absent 'Internal error: Oops'            'Oops'
check_absent 'BUG: unable to handle'           'BUG'

# --- 预期内的 panic（QEMU 没有 rootfs，与硬件无关）---
if grep -q 'VFS: Unable to mount root fs' "$WORK/console.log"; then
  echo "  [OK] 以 'VFS: Unable to mount root fs' 结束（QEMU 无 rootfs，属预期）"
fi

# --- 集成能力（若有）应已初始化 ---
if grep -q 'susfs is initialized' "$WORK/console.log"; then
  ver=$(grep -oE 'susfs is initialized! version: [^ ]+' "$WORK/console.log" | head -1)
  echo "  [OK] $ver"
fi
if grep -q 'KernelSU version:' "$WORK/console.log"; then
  ver=$(grep -oE 'KernelSU version: [^ ]+' "$WORK/console.log" | head -1)
  echo "  [OK] $ver"
fi
if grep -q 'preinit: runner staged' "$WORK/console.log"; then
  echo "  [OK] FinaSU preinit runner 已注入"
fi

[ "$fail" -eq 0 ] || { echo "::error::QEMU 早期启动验证失败"; exit 1; }
echo "[qemu] PASS —— 内核早期启动健康"
