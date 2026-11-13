#!/usr/bin/env bash
# 应用「自动跟随上游」生成的 SUSFS 补丁，并严格校验每个 hunk 是否真的落位。
#
# 为什么不能只 `patch -p1 < patch`：
#   patch 在上下文不符时会**跳过 hunk 而不报错**（有时连 .rej 都不写），结果是
#   「补丁看起来应用成功，实际少了若干段代码」——例如 fs/stat.c 的 SUS_KSTAT
#   伪装、fs/proc/bootconfig.c 的 verifiedbootstate 保留。这类缺失不会立刻编译
#   失败，而是功能静默失效，比直接报错危险得多。
#
# 断言怎么来：**从补丁自身提取**（每个文件挑一条最独特的新增行），
# 而不是手写在脚本里。手写断言会随上游版本过时——本项目就踩过：
# 拿 2.3.0 时代才有的 DEFAULT_KSU_MNT_ID 去断言 2.4.0，误判成「hunk 丢失」。
#
# 用法：bash scripts/apply_susfs.sh <susfs 输出目录> <内核源码目录>
set -euo pipefail

SRC="${1:?用法: apply_susfs.sh <susfs 输出目录> <内核源码目录>}"
KERNEL="${2:?用法: apply_susfs.sh <susfs 输出目录> <内核源码目录>}"
PATCH="$SRC/susfs.patch"
[ -f "$PATCH" ] || { echo "找不到 $PATCH"; exit 1; }

# 先核对：待应用的补丁是否就是 fetch 阶段记录的那份（防止拿到半截/被改）
if [ -f "$SRC/susfs-meta.env" ]; then
  want="$(sed -n 's/^patch_sha256=//p' "$SRC/susfs-meta.env")"
  got="$(sha256sum "$PATCH" | cut -d' ' -f1)"
  [ "$want" = "$got" ] || { echo "::error::susfs.patch sha256 与元数据不符"; exit 1; }
  echo "[OK] 补丁 sha256 校验通过（${got:0:12}）"
fi

cd "$KERNEL"

# --- 幂等：已经打过就跳过（避免「Reversed patch detected」把整批 hunk 全丢掉）---
if grep -q 'susfs_is_current_ksu_domain' security/selinux/status.c 2>/dev/null \
   && grep -q 'susfs_spoof_cmdline_or_bootconfig' fs/proc/bootconfig.c 2>/dev/null; then
  echo "[skip] SUSFS 已应用，跳过"
  exit 0
fi

echo "=== 应用 SUSFS 补丁 ==="
set +e
patch -p1 --no-backup-if-mismatch < "$PATCH" > /tmp/susfs-apply.log 2>&1
set -e
grep -E 'FAILED|rej|fuzz|succeeded|patching file' /tmp/susfs-apply.log | tail -40 || true

fail=0

# --- .rej 检查 ---
rejs="$(find . -name '*.rej' | sort)"
if [ -n "$rejs" ]; then
  echo "::error::存在 .rej，说明有 hunk 未应用："; echo "$rejs"; fail=1
else
  echo "[OK] 无 .rej"
fi

# --- 从补丁自身生成断言并逐条校验 ---
echo "=== 关键 hunk 落位断言（指纹由补丁动态提取）==="
python3 - "$PATCH" "$KERNEL" "$fail" <<'PY'
import re, sys, pathlib

patch_path, kernel, fail_s = sys.argv[1], sys.argv[2], sys.argv[3]
fail = int(fail_s)

text = pathlib.Path(patch_path).read_text(encoding='utf-8', errors='replace')
# 按 diff --git 切块
parts = re.split(r'^diff --git a/(\S+) b/\S+$', text, flags=re.M)
# 通用宏/头文件噪声：太常见，做指纹会假阳性
NOISE = (
    'CONFIG_KSU', 'susfs_def.h', '#include', '#ifdef', '#endif', '#else',
    'extern ', 'susfs.h', '};', 'struct ', 'return ', 'NULL', 'true', 'false',
)

def pick_fingerprint(added):
    """挑一条最有辨识度的新增行：最长、且不属于噪声、含 susfs/ksu 标识。"""
    cands = []
    for ln in added:
        s = ln.strip()
        if len(s) < 12 or not s or s.startswith('//'):
            continue
        if any(s.startswith(n) for n in NOISE):
            continue
        if not ('susfs' in s or 'ksu' in s or 'SUSFS' in s or 'KSU' in s):
            continue
        cands.append(s)
    if not cands:
        return None
    # 优先「函数调用」形态，其次最长
    cands.sort(key=lambda s: (('(' in s), len(s)), reverse=True)
    return cands[0]

checked = 0
for i in range(1, len(parts) - 1, 2):
    fname = parts[i]
    body = parts[i + 1]
    added = [l[1:] for l in body.splitlines()
             if l.startswith('+') and not l.startswith('+++')]
    fp = pick_fingerprint(added)
    if fp is None:
        continue
    target = pathlib.Path(kernel) / fname
    if not target.exists():
        print(f'  [FAIL] {fname} 不存在（补丁引用了不存在的文件）')
        fail = 1
        continue
    content = target.read_text(encoding='utf-8', errors='replace')
    checked += 1
    if fp in content:
        print(f'  [OK]   {fname}')
    else:
        print(f'  [FAIL] {fname} 缺少指纹行：{fp[:90]}')
        fail = 1

print(f'\n共校验 {checked} 个文件')
sys.exit(1 if fail else 0)
PY
rc=$?

if [ "$rc" -ne 0 ]; then
  echo
  echo "::error::SUSFS 补丁未完整应用。可能原因：上游上下文与当前内核不符，"
  echo "  需要给 scripts/fetch_susfs.sh 增加新的 fixup（参考 muling0721/GKI_FinaSU、"
  echo "  xuyi726/ak3_XHigh 的 scripts/susfs_fixes/apply.sh）。"
  echo "  应用日志：/tmp/susfs-apply.log"
  exit 1
fi

echo "[OK] SUSFS 补丁完整应用"
