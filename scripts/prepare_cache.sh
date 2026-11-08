#!/usr/bin/env bash
# 准备编译缓存：下载 GKI 源码 + clang 工具链，校验内容，分卷（每个 ≤90 MB，
# 因为 GitHub git push 对单文件有 100 MB 硬限制），并生成 cache/ 目录树。
#
# 用法：  Git 环境变量 GKI_SHA / GKI_URL / CLANG_URL / CLANG_ID 由 workflow 提供
#         bash scripts/prepare_cache.sh <输出目录>
#
# 产物布局（会被推到仓库的 `cache` 分支）：
#   cache/README.md
#   cache/manifest.txt                 总索引（pin、生成时间、各 key 的 tarball 摘要）
#   cache/gki/manifest.txt             key 描述 + 分卷清单
#   cache/gki/gki-<sha12>.tar.gz.part.00 ...
#   cache/clang/manifest.txt
#   cache/clang/clang-r487747c.tar.gz.part.00 ...
set -euo pipefail

OUT="${1:?用法: prepare_cache.sh <输出目录>}"
GKI_SHA="${GKI_SHA:?需要 GKI_SHA}"
GKI_URL="${GKI_URL:?需要 GKI_URL}"
CLANG_URL="${CLANG_URL:?需要 CLANG_URL}"
CLANG_ID="${CLANG_ID:?需要 CLANG_ID}"

TMP="${RUNNER_TEMP:-/tmp}/prep-cache"
rm -rf "$TMP"
mkdir -p "$TMP" "$OUT"
CHUNK=90000000   # 90 MB，留出余量给 git 的 100 MB 单文件限制

echo "=== 下载 GKI 源码（$GKI_URL）==="
curl -sSL --retry 5 --retry-delay 5 -o "$TMP/gki.tar.gz" "$GKI_URL"
echo "=== 下载 clang（$CLANG_URL）==="
curl -sSL --retry 5 --retry-delay 5 -o "$TMP/clang.tar.gz" "$CLANG_URL"
ls -la "$TMP/gki.tar.gz" "$TMP/clang.tar.gz"

echo "=== 校验 GKI 源码内容（不是只信任下载）==="
mkdir -p "$TMP/v"
tar -xzf "$TMP/gki.tar.gz" -C "$TMP/v" Makefile
V="$(sed -n 's/^VERSION = //p' "$TMP/v/Makefile")"
P="$(sed -n 's/^PATCHLEVEL = //p' "$TMP/v/Makefile")"
S="$(sed -n 's/^SUBLEVEL = //p' "$TMP/v/Makefile")"
echo "GKI 版本: $V.$P.$S"
test "$V.$P.$S" = "6.1.25" || { echo "GKI 源码不是 6.1.25"; exit 1; }

echo "=== 校验 clang 工具链 ==="
mkdir -p "$TMP/c"
tar -xzf "$TMP/clang.tar.gz" -C "$TMP/c"
CLANG="$(find "$TMP/c" -type f -name clang -path '*/bin/*' | head -1)"
test -n "$CLANG" || { echo "解压后找不到 clang"; exit 1; }
"$CLANG" --version | head -2
"$CLANG" --version | grep -q "17.0.2" || { echo "clang 版本不是 17.0.2"; exit 1; }

# ---- 分卷 + 写 manifest ----
make_key() {
  local key="$1" src="$2" suffix="$3"
  local sha size short dir tarball n
  sha="$(sha256sum "$src" | cut -d' ' -f1)"
  size="$(stat -c%s "$src")"
  short="${sha:0:12}"
  tarball="${key}-${short}.tar.gz"
  dir="$OUT/$key"
  mkdir -p "$dir/parts"
  # 分卷（覆盖旧内容）
  rm -f "$dir/parts/"*
  split -b "$CHUNK" -d -a 2 "$src" "$dir/parts/${tarball}.part."
  {
    echo "key=$key"
    echo "tarball=$tarball"
    echo "sha256=$sha"
    echo "size=$size"
    echo "parts=$(ls "$dir/parts/" | wc -l)"
    for f in "$dir/parts/"*; do
      psha="$(sha256sum "$f" | cut -d' ' -f1)"
      psize="$(stat -c%s "$f")"
      echo "part=$(basename "$f") $psize $psha"
    done
  } > "$dir/manifest.txt"
  echo "[cache] $key: $tarball ($size B, $(ls "$dir/parts/" | wc -l) 卷)"
  echo "$key|$tarball|$sha|$size|$(ls "$dir/parts/" | wc -l)" >> "$TMP/summary.txt"
}

echo "=== 分卷 ==="
make_key gki "$TMP/gki.tar.gz" gki
make_key clang "$TMP/clang.tar.gz" clang

echo "=== 写总 manifest / README ==="
{
  echo "# 编译缓存（由 .github/workflows/prepare-cache.yml 自动生成，勿手改）"
  echo "generated_at=$(date -u +%FT%TZ)"
  echo "generator=${GITHUB_REPOSITORY:-local}@${GITHUB_SHA:-unknown}"
  echo "gki_sha=$GKI_SHA"
  echo "clang_id=$CLANG_ID"
  echo "chunk_size=$CHUNK"
  echo "# key|tarball|sha256|size|parts"
  cat "$TMP/summary.txt"
} > "$OUT/manifest.txt"

cat > "$OUT/README.md" <<'EOF'
# 编译缓存（`cache` 分支）

这个分支只放**编译时可复用的上游内容**，用来跳过每次从 Google 服务器重新下载：

| 目录 | 内容 | 来源 |
|---|---|---|
| `cache/gki/` | AOSP `kernel/common` @ pin SHA 的 tar.gz（分卷） | android.googlesource.com `+archive` |
| `cache/clang/` | `clang-r487747c`（Android clang 17.0.2）tar.gz（分卷） | prebuilts/clang `android14-release` |

规则：
- 每个文件 ≤90 MB（GitHub git push 单文件 100 MB 硬限制），用 `split` 分卷；
- 使用方（`meizu21.yml`）用 `scripts/restore_cache.sh` 恢复，并**逐卷 + 整包校验 sha256**；
- 校验失败或分支不存在 → 自动回退到从 Google 直接下载，不会卡死构建；
- **FinaSU 不在缓存里**：每次构建实时从 GitHub 拉最新 release（源码 + 管理器 APK + 证书）。

刷新方式：改 `cache-trigger.txt` 并 push 到 `main`（或手动跑 `prepare-cache.yml`）。
EOF

echo "=== 完成 ==="
cat "$OUT/manifest.txt"
du -sh "$OUT"
