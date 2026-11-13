#!/usr/bin/env bash
# FinaSU ⇄ 本内核补丁 的「前向兼容」预检。
#
# 背景：FinaSU 每次构建取最新 release（用户要求），而上游可能随时新增
# 「要求内核侧提供某符号/某入口」的代码。本脚本把这类隐式依赖变成显式检查，
# 避免新版本把一个原本能编过的树突然打断到编译期深处。
#
# 用法： bash scripts/check_finasu_compat.sh <FinaSU 目录> <内核源码目录>
#   退出码 0 = 兼容；1 = 有硬依赖缺失（应当中止构建，或改用 finasu_ref 固定旧版本）
set -euo pipefail

FS="${1:?用法: check_finasu_compat.sh <FinaSU 目录> <kernel 树>}"
KERNEL="${2:?用法: check_finasu_compat.sh <FinaSU 目录> <kernel 树>}"

fail=0
warn=0

say_fail() { echo "  [FAIL] $*"; fail=1; }
say_warn() { echo "  [WARN] $*"; warn=1; }
say_ok()   { echo "  [ OK ] $*"; }

echo "=== FinaSU 版本 ==="
if [ -d "$FS/.git" ]; then
  echo "  commit=$(git -C "$FS" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "  tag=$(git -C "$FS" describe --tags --always 2>/dev/null || echo unknown)"
fi

echo "=== 1. SUSFS Inline 入口自检（上游自带脚本）==="
if sh "$FS/kernel/tools/check_susfs_inline.sh" "$KERNEL" >/tmp/fs_inline.log 2>&1; then
  say_ok "check_susfs_inline.sh 通过"
else
  say_fail "check_susfs_inline.sh 失败："
  sed 's/^/        /' /tmp/fs_inline.log || true
fi

echo "=== 2. FinaSU Kconfig 里的 SUSFS 子选项 vs 内核侧提供者 ==="
# 上游每加一个 SUSFS 子选项，通常对应内核侧一个 susfs_* 符号；这里按「选项名 → 提供者」映射检查。
check_provider() {
  local cfg="$1" sym="$2" hint="$3"
  if grep -rqw "$sym" "$KERNEL/fs/susfs.c" "$KERNEL/include/linux/susfs.h" \
        "$KERNEL/include/linux/susfs_def.h" 2>/dev/null; then
    say_ok "$cfg 需要的内核侧 $sym 存在"
  else
    say_warn "$cfg 需要的内核侧 $sym 不存在（$hint）"
  fi
}
FS_KCONFIG="$FS/kernel/Kconfig"
if [ -f "$FS_KCONFIG" ]; then
  if grep -q '^config KSU_SUSFS_PRIORITY_UID_FILTER' "$FS_KCONFIG"; then
    # 该选项启用后 FinaSU 会直接调用 susfs_parse_priority_app_strings()，必须是
    # 非 static 的全局符号（我们的参考树版本已满足，见 kernel-add/fs/susfs.c）
    if grep -q '^void susfs_parse_priority_app_strings(void)' "$KERNEL/fs/susfs.c" 2>/dev/null; then
      say_ok "KSU_SUSFS_PRIORITY_UID_FILTER 需要的内核侧 susfs_parse_priority_app_strings 存在（全局符号）"
    elif grep -q 'susfs_parse_priority_app_strings' "$KERNEL/fs/susfs.c" 2>/dev/null; then
      say_fail "susfs_parse_priority_app_strings 存在但不是全局符号（被 static 了），FinaSU 新版会链接失败"
    else
      say_fail "KSU_SUSFS_PRIORITY_UID_FILTER 需要的内核侧 susfs_parse_priority_app_strings 缺失"
    fi
    grep -rqw 'CMD_SUSFS_SET_PRIORITY_UID_FILTER' "$KERNEL/include/linux/susfs_def.h" \
      && say_ok "CMD_SUSFS_SET_PRIORITY_UID_FILTER 存在" \
      || say_fail "CMD_SUSFS_SET_PRIORITY_UID_FILTER 缺失（extension/susfs.c 会引用）"
  fi
fi

echo "=== 3. FinaSU Kbuild 的内核侧探测项 ==="
# Kbuild 用 grep 探测内核特性；探测不到的会关闭对应调用点（安全，但要知道）
for probe in susfs_add_sus_type; do
  if grep -q "$probe" "$FS/kernel/Kbuild" 2>/dev/null; then
    if grep -rqw "$probe" "$KERNEL/fs/susfs.c" 2>/dev/null; then
      say_ok "Kbuild 探测 $probe → 命中（会启用更多特性）"
    else
      say_warn "Kbuild 探测 $probe → 未命中（相关调用点会被编译掉，属安全降级）"
    fi
  fi
done

echo "=== 4. 构建期必需配置项（FinaSU 硬要求）==="
CFG="$KERNEL/.config"
if [ -f "$CFG" ]; then
  for opt in CONFIG_KSU CONFIG_KALLSYMS_ALL CONFIG_KPROBES CONFIG_EXT4_FS \
             CONFIG_THREAD_INFO_IN_TASK CONFIG_HEADERS_INSTALL CONFIG_KSU_SUSFS; do
    grep -q "^$opt=y" "$CFG" && say_ok "$opt=y" || say_fail "$opt 未开启"
  done
  if grep -q '^config KSU_SUSFS_PRIORITY_UID_FILTER' "$FS_KCONFIG" 2>/dev/null; then
    grep -q '^CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y' "$CFG" \
      && say_ok "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y（新版 FinaSU 需要）" \
      || say_warn "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER 未开（olddefconfig 应默认 y，检查 Kconfig 是否已集成）"
  fi
else
  say_warn "未找到 $CFG，跳过配置检查（应在 olddefconfig 之后运行本脚本）"
fi

echo "=== 5. inline hook 后端选择 ==="
if [ -f "$CFG" ]; then
  if grep -q '^CONFIG_KSU_HOOK_SUSFS_INLINE=y' "$CFG"; then
    say_ok "hook 后端 = SUSFS Inline"
  elif grep -q '^CONFIG_KSU_HOOK_TRACEPOINT=y' "$CFG"; then
    say_ok "hook 后端 = Tracepoint"
  else
    say_fail "两种 hook 后端都没开"
  fi
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "[FINASU COMPAT] FAILED —— 当前 FinaSU 与内核补丁不兼容。"
  echo "  处理方式：① 更新 patches/ 与 kernel-add/ 里的 SUSFS 到匹配版本；"
  echo "            ② 或 dispatch 时把 finasu_ref 固定到上一可用版本（如 v1.5.0）。"
  exit 1
fi
if [ "$warn" -ne 0 ]; then
  echo "[FINASU COMPAT] PASSED（有降级提示，见上）"
else
  echo "[FINASU COMPAT] PASSED (100%)"
fi
