#!/usr/bin/env bash
# 从仓库的 `cache` 分支恢复编译缓存（GKI 源码 / clang 工具链）。
#
# 用法：  bash scripts/restore_cache.sh <gki|clang> <目标目录>
#         （在 actions/checkout 之后、仓库根目录执行；需要 origin 可 fetch）
#
# 退出码：0 = 成功恢复；1 = 没有缓存或校验失败（调用方应回退到从 Google 下载）
set -euo pipefail

KEY="${1:?用法: restore_cache.sh <gki|clang> <destdir>}"
DEST="${2:?用法: restore_cache.sh <gki|clang> <destdir>}"
WS="${GITHUB_WORKSPACE:-$(pwd)}"
TMP="${RUNNER_TEMP:-/tmp}/repo-cache-$KEY"

rm -rf "$TMP"
mkdir -p "$TMP"

if ! git -C "$WS" fetch --depth 1 origin cache >/dev/null 2>&1; then
  echo "[cache] 仓库里没有 cache 分支（首次构建属正常）"
  exit 1
fi
REF="$(git -C "$WS" rev-parse FETCH_HEAD)"
if ! git -C "$WS" archive "$REF" "cache/$KEY" 2>/dev/null | tar -x -C "$TMP" --strip-components=2; then
  echo "[cache] cache 分支里没有 cache/$KEY"
  exit 1
fi

M="$TMP/manifest.txt"
[ -f "$M" ] || { echo "[cache] 缺少 manifest.txt"; exit 1; }

tarball=""; expect_sha=""; expect_size=""
part_lines=()
while IFS='=' read -r k v; do
  case "$k" in
    tarball) tarball="$v" ;;
    sha256)  expect_sha="$v" ;;
    size)    expect_size="$v" ;;
    part)    part_lines+=("$v") ;;
  esac
done < "$M"

[ -n "$tarball" ] && [ -n "$expect_sha" ] && [ "${#part_lines[@]}" -gt 0 ] \
  || { echo "[cache] manifest 不完整（tarball/sha256/part 缺项）"; exit 1; }

TARBALL="$TMP/$tarball"
: > "$TARBALL"
for line in "${part_lines[@]}"; do
  # 格式：<分卷名> <大小> <sha256>
  read -r pname psize psha <<< "$line"
  f="$TMP/parts/$pname"
  [ -f "$f" ] || { echo "[cache] 缺分卷 $pname"; exit 1; }
  got_psha="$(sha256sum "$f" | cut -d' ' -f1)"
  [ "$got_psha" = "$psha" ] || { echo "[cache] 分卷校验失败: $pname"; exit 1; }
  cat "$f" >> "$TARBALL"
done

got_sha="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
got_size="$(stat -c%s "$TARBALL")"
[ "$got_sha" = "$expect_sha" ] || { echo "[cache] 整包 sha256 不符"; exit 1; }
[ "$got_size" = "$expect_size" ] || { echo "[cache] 整包大小不符 ($got_size != $expect_size)"; exit 1; }

mkdir -p "$DEST"
tar -xzf "$TARBALL" -C "$DEST"
echo "[cache] 已恢复 $KEY → $DEST ($(du -sh "$DEST" | cut -f1))"
