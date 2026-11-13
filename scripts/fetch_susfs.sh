#!/usr/bin/env bash
# 自动跟随上游 SUSFS：拉取上游 patch + 三个源文件，按目标内核版本修补上下文，
# 生成可直接应用到我们这棵树的 SUSFS 补丁。
#
# 背景：上游 susfs4ksu 的 50-patch 是针对「较新 ACK」生成的，直接应用到
# GKI r17（6.1.25）会因 include 上下文差异产生 .rej。两个独立项目
# （muling0721/GKI_FinaSU、xuyi726/ak3_XHigh）都用同一套「应用前修上下文」的做法，
# 本脚本把那套经验收敛到这里，并按我们的 pin/内核实际状态做条件判断。
#
# 用法：
#   bash scripts/fetch_susfs.sh <输出目录> [--ref <branch-or-commit>]
#
# 输出：
#   <输出目录>/susfs.patch           已修好上下文的补丁（可直接 git apply / patch -p1）
#   <输出目录>/fs/susfs.c            上游 SUSFS_VERSION 对应的源文件
#   <输出目录>/include/linux/susfs.h
#   <输出目录>/include/linux/susfs_def.h
#   <输出目录>/susfs-meta.env        版本/提交/修复记录（供 CI 展示与产物元数据）
set -euo pipefail

OUT="${1:?用法: fetch_susfs.sh <输出目录> [--ref <branch-or-commit>]}"
shift || true
REF="gki-android14-6.1"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

SUSFS_REPO="${SUSFS_REPO:-https://gitlab.com/simonpunk/susfs4ksu.git}"
# 允许用 GitHub 镜像（本机 GitLab 偶尔慢）
SUSFS_MIRROR="${SUSFS_MIRROR:-https://github.com/JackEblan/susfs4ksu.git}"

KERNEL_SRC="${KERNEL_SRC:-$(pwd)/common}"
PATCH_DIR="${PATCH_DIR:-$(pwd)/patches}"
FIXUP_LOG="$OUT/susfs-fixups.log"

mkdir -p "$OUT/fs" "$OUT/include/linux"
: > "$FIXUP_LOG"

log() { echo "$*" | tee -a "$FIXUP_LOG"; }

# ---------------------------------------------------------------- 取上游源码
# 优先用 git 精确取 ref（能拿到真实 commit 并用于版本记录）；失败再退回 raw 下载。
TMP_REPO="${RUNNER_TEMP:-/tmp}/susfs-src"
rm -rf "$TMP_REPO"
log "=== 拉取上游 SUSFS ($REF) ==="
if git clone -q --depth 1 --branch "$REF" "$SUSFS_REPO" "$TMP_REPO" 2>/dev/null \
   || git clone -q --depth 1 --branch "$REF" "$SUSFS_MIRROR" "$TMP_REPO" 2>/dev/null; then
  SUSFS_COMMIT="$(git -C "$TMP_REPO" rev-parse HEAD)"
  log "commit=$SUSFS_COMMIT"
  KP="$TMP_REPO/kernel_patches"
  cp "$KP/50_add_susfs_in_gki-android14-6.1.patch" "$OUT/susfs.patch"
  cp "$KP/fs/susfs.c"                       "$OUT/fs/susfs.c"
  cp "$KP/include/linux/susfs.h"            "$OUT/include/linux/susfs.h"
  cp "$KP/include/linux/susfs_def.h"        "$OUT/include/linux/susfs_def.h"
else
  log "git clone 失败，退回 raw 下载"
  RAW="${SUSFS_REPO}/-/raw/${REF}"
  curl -sSL --retry 3 -o "$OUT/susfs.patch"                  "$RAW/kernel_patches/50_add_susfs_in_gki-android14-6.1.patch"
  curl -sSL --retry 3 -o "$OUT/fs/susfs.c"                   "$RAW/kernel_patches/fs/susfs.c"
  curl -sSL --retry 3 -o "$OUT/include/linux/susfs.h"        "$RAW/kernel_patches/include/linux/susfs.h"
  curl -sSL --retry 3 -o "$OUT/include/linux/susfs_def.h"    "$RAW/kernel_patches/include/linux/susfs_def.h"
  SUSFS_COMMIT="unknown"
fi

SUSFS_VERSION="$(sed -n 's/^#define SUSFS_VERSION "\(.*\)"/\1/p' "$OUT/include/linux/susfs.h" | head -1)"
log "SUSFS_VERSION=$SUSFS_VERSION"
[ -s "$OUT/susfs.patch" ] || { echo "补丁下载失败"; exit 1; }

# ------------------------------------------------------------ 读内核版本信息
SUBLEVEL="$(sed -n 's/^SUBLEVEL = //p' "$KERNEL_SRC/Makefile" | head -1)"
OS_PATCH="2023-10"   # 我们的 pin：android14-6.1-2023-10
log "目标内核: 6.1.$SUBLEVEL (os_patch=$OS_PATCH)"

# ============================================================ 上下文修复
# 每一项都带「为什么」，便于上游变化时判断是否还成立。

# --- 修复 1：VMA_PAD_START 兼容 -------------------------------------------
# 上游把 show_map_vma() 的 end 写成 VMA_PAD_START(vma)，但该宏只存在于带
# VM_PAD 回移的厂商树；标准 ACK（含我们的 r17）没有它 → -Wimplicit-function-declaration。
# 两个独立项目（muling0721 / xuyi726）都用同一修法，后者注释点名 6.1.25~68。
if grep -qF 'VMA_PAD_START(vma)' "$OUT/susfs.patch" \
   && ! grep -Rqs 'VMA_PAD_START' "$KERNEL_SRC/include/linux"; then
  log "[fixup] 目标内核无 VMA_PAD_START → 替换为 vma->vm_end"
  sed -i 's/VMA_PAD_START(vma)/vma->vm_end/g' "$OUT/susfs.patch"
fi

# --- 修复 2：fs/proc/base.c 的 include 锚点 -------------------------------
# 2.4.0 的 base.c hunk 以 `#include <linux/dma-buf.h>` 与
# `#include <trace/hooks/sched.h>` 作为上下文；r17 缺这两行 → hunk 靠 fuzz
# 或整段失配。做法：先（临时）把缺的 include 补进源树，让补丁能精确匹配，
# 应用后再由 CI 还原（脚本只改源树，不改补丁文本，避免重算行号）。
BASE_C="$KERNEL_SRC/fs/proc/base.c"
ADDED_INCLUDES=()
if [ -f "$BASE_C" ]; then
  if ! grep -qF '#include <linux/dma-buf.h>' "$BASE_C"; then
    sed -i '/^#include <linux\/cpufreq_times.h>$/a #include <linux/dma-buf.h>' "$BASE_C" \
      && ADDED_INCLUDES+=("dma-buf.h")
    log "[fixup] base.c 补 include <linux/dma-buf.h>（hunk 锚点）"
  fi
  if ! grep -qF '#include <trace/hooks/sched.h>' "$BASE_C"; then
    sed -i '/^#include <trace\/events\/oom.h>$/a #include <trace/hooks/sched.h>' "$BASE_C" \
      && ADDED_INCLUDES+=("trace/hooks/sched.h")
    log "[fixup] base.c 补 include <trace/hooks/sched.h>（hunk 锚点）"
  fi
fi

# --- 修复 3：fs/namespace.c 的 blk.h 上下文 ------------------------------
# 2026-10-19 起的补丁把 `#include <trace/hooks/blk.h>` 当上下文行（要求存在）。
# r17 没有这行 → 补上；**不能删**（删了反而 hunk 失配，ak3_XHigh 的注释记了这个坑）。
NS_C="$KERNEL_SRC/fs/namespace.c"
if [ -f "$NS_C" ] && grep -q '^ #include <trace/hooks/blk\.h>' "$OUT/susfs.patch"; then
  if ! grep -qF '#include <trace/hooks/blk.h>' "$NS_C"; then
    sed -i '/^#include "internal.h"$/a #include <trace/hooks/blk.h>' "$NS_C" \
      && ADDED_INCLUDES+=("trace/hooks/blk.h(ns)")
    log "[fixup] namespace.c 补 include <trace/hooks/blk.h>（hunk 上下文行）"
  fi
fi

# --- 修复 4：super.c 旧上下文（仅当补丁仍含 thaw_super_locked 锚点）------
SUPER_C="$KERNEL_SRC/fs/super.c"
SUPER_H_REMOVED=0
if [ -f "$SUPER_C" ] && grep -q '^ static int thaw_super_locked' "$OUT/susfs.patch" \
   && grep -qF '#include <trace/hooks/fs.h>' "$SUPER_C"; then
  log "[fixup] super.c 临时移除 trace/hooks/fs.h（旧版补丁上下文依赖）"
  sed -i '/^#include <trace\/hooks\/fs.h>$/,+1d' "$SUPER_C"
  SUPER_H_REMOVED=1
fi

# --- 修复 5：mm/mmap.c VM_PAD_MASK（仅 2024-11 分支）---------------------
if ls "$KERNEL_SRC"/mm/mmap.c >/dev/null 2>&1 && [ "$OS_PATCH" = "2024-11" ] \
   && grep -qF 'vm_flags_clear(new_vma, VM_PAD_MASK);' "$KERNEL_SRC/mm/mmap.c"; then
  log "[fixup] mmap.c VM_PAD_MASK 用位运算替代缺失的 vm_flags_clear"
  sed -i 's/vm_flags_clear(new_vma, VM_PAD_MASK);/new_vma->vm_flags \&= ~VM_PAD_MASK;/' "$KERNEL_SRC/mm/mmap.c"
fi

# ---------------------------------------------------------------- 元数据
{
  echo "susfs_ref=$REF"
  echo "susfs_commit=$SUSFS_COMMIT"
  echo "susfs_version=$SUSFS_VERSION"
  echo "kernel_sublevel=6.1.$SUBLEVEL"
  echo "os_patch_level=$OS_PATCH"
  echo "added_includes=${ADDED_INCLUDES[*]:-none}"
  echo "super_fs_h_removed=$SUPER_H_REMOVED"
  echo "patch_sha256=$(sha256sum "$OUT/susfs.patch" | cut -d' ' -f1)"
  echo "generated_at=$(date -u +%FT%TZ)"
} > "$OUT/susfs-meta.env"

log "=== 完成：$SUSFS_VERSION @ ${SUSFS_COMMIT:0:12} ==="
cat "$OUT/susfs-meta.env" | tee -a "$FIXUP_LOG"
