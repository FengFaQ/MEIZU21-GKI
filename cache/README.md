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
