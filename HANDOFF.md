# 交接文档 · 魅族 21 GKI

> 给下一个接手的人 / AI。目标：30 分钟内知道「这是什么 / 现在到哪 / 下一步 / 别碰什么」。
> 最后更新：2026-11-15

---

## 0. 一句话

用 **GKI android14-6.1（6.1.25 / os_patch 2023-10）+ 21 Pro 参考树的兼容层补丁 + FinaSU 内建**，
在 GitHub Actions 里编出魅族 21（M2461）的 `Image`，再重打包成 **与可开机参考镜像同构、不带 AVB** 的
96 MiB `boot.img`（只替换 boot 分区）。

## 1. 当前状态（2026-11-15 更新：重打包方式已按可开机参考镜像改正）

> **2026-11-15 关键更正**：此前 run#11~run#23 的 boot.img 一律「自带 AVB」——要么塞原厂 stale vbmeta，
> 要么用自有密钥重签。两者都必然被引导器拒绝（详见 `MEMORY.md §1.4`）。现已改为**完全不写 AVB**，
> 与实测可开机的参考镜像 `resukisu-boot.img` 结构一致，并有逐字节回归测试。

| 项 | 状态 |
|---|---|
| 设备事实（config / KMI / kCFI / boot 头） | ✅ 齐全（`device/meizu21/`） |
| CI 完整流水线 | ✅ **run#27 全绿**（24 步：缓存 → 编译 → 三门禁 → 重打包 → QEMU → release + dist） |
| 重打包结构 | ✅ **已改正**：无 AVB、无签名、原厂头 + kernel + 零填充 |
| 回归测试 | ✅ 参考 kernel 重打包 → 与 `resukisu-boot.img` **逐字节相同**（sha256 `3eb02d29…61bb`） |
| 模块加载门禁 | ✅ 696 个 vendor .ko 的 `module_layout` 全为 `0xea759d7f`，与本构建一致 |
| 真机刷入验证 | ❌ 未做（下一步，只刷非活动槽） |

### run#27 实测产物（2026-11-15，可直接刷）

| 文件 | 大小 | sha256 |
|---|---|---|
| `boot.img` | 100,663,296 | `78ce8473ee45336e419e814cdc57430f1ba46cd2a4d49133e60e8c6de04c02b3` |
| `Image` | 34,957,824 | `686737ec9486a98d53298e3a81cc31c5cf69afef377edea460cc32767c8e19ea` |
| `FinaSU_v1.6.2_33203-release.apk` | 28,711,012 | `61fea3ba60e78d43638c8839ff88f79e409405cb74b53acc1791faac1a8840ab` |

`boot_avb_report.txt` 自检全绿：`hdr diff offsets [9, 10]` / `hdr only kernel_size True` /
`payload_end aligned True` / `tail_zero_filled True` / `no AVB0 magic True` /
`has AVBf @EOF-64 False` / `has AVBf anywhere False`。
独立复核：`boot.img.find(b'AVB0') == -1`、`find(b'AVBf') == -1`、尾部 64 B 全零。

三门禁（run#27）：gate1 `release_actual=6.1.25-android14-11-maybe-dirty`；
gate2 `(stock-built) ∩ NEED = 0`、built exported 14,688；
gate3 `21 MATCH, 0 MISMATCH, 3 N/A`。

### 本次修复的产物结构（`scripts/make_boot_zeroavb.py`）

- 100,663,296 B（= 原厂 boot 分区大小，便于 `fastboot flash` 顺带清掉旧 AVBf 残留）；
- header v4 / header_size 1584 / ramdisk 0 / os_version `0x1C00017A` / cmdline 空 / signature_size 0；
- 与原厂头页的**唯一差异是 `kernel_size`（偏移 8~11）**；
- `[0x1000, 0x1000+kernel_size)` 为 Image，其后全零；**无 `AVB0`、`AVBf` 处无 footer**；
- `--bare` 可精确复现参考镜像的尺寸（34,369,536 B 那一类，不补齐分区）。

### 下一步

1. 刷 run#27 的 `boot.img`（sha256 `78ce8473…02b3`），用 `docs/FLASH_SOP.md` 的步骤**只刷非活动槽**。
2. 首刷后按 SOP 回填 dmesg（kCFI `expected type`、Unknown symbol、`version magic`）。
3. 若仍不能开机，按 `docs/FLASH_DIAGNOSIS.md` 的 B1→B2→B3 逐级剥离：
   `hook_backend=tracepoint` → 关 `CONFIG_KSU_SUSFS` → 关 `CONFIG_KSU`（纯 GKI 能否开机是关键判据）。
4. 真机通过后，`tools/fetch_dist.ps1` 可随时取回最新产物。

## 2. 仓库结构

```
.github/workflows/meizu21.yml   唯一入口（手动 dispatch；inputs 4 个）
patches/01-kernel-common.patch  21 Pro 参考树的兼容层，已按 M2461 改名（config 文件名 + 注释）
kernel-add/fs/susfs.c           SUSFS 内核实现（susfs4ksu @27e0a0e）
kernel-add/include/linux/susfs.h, susfs_def.h
device/meizu21/                 设备事实（见 README）
scripts/                        门禁 + 重打包（check_kcfi / check_krcrc_symvers / make_boot_zeroavb / extract_manager_cert）
scripts/archive/                已废弃的 AVB 重签/重打包脚本（*.deprecated，勿用）
docs/FLASH_SOP.md               刷机 / 回退 / 故障诊断
```

## 3. 关键 pin（改任何一个都要重做验证）

| 项 | 值 |
|---|---|
| GKI 源码 | `kernel/common` @ `0d89215310919593150f0ef047f9fd9902e02bc7`（`android14-6.1-2023-10_r17`） |
| 工具链 | `clang-r487747c`（Android clang 17.0.2），AOSP prebuilts 分支 `android14-release` |
| FinaSU | 流水线按 `latest` 实时拉取；run#27 为 `v1.6.2` = `6cc294cc530eed32a10ff4d841af83c1bc0c2dc4`；APK sha256 `61fea3ba…40ab` |
| 管理器证书 | `0x02cc` / `2d89b50aaf1db013c2b6738f973f48b62ea21a3a8d7216bfec3521065d448458` |
| 原厂 boot | sha256 `8c899eb3…4619f5`，100,663,296 B |
| 原厂 config | 从原厂 boot.img 的 IKCONFIG 提取（7430 行） |

## 4. 已踩过 / 已验证的坑（详见 `MEMORY.md`）

- 参考树 `C:\GKI\m2481-kernel` **只能读不能编**（Windows 拷贝把 symlink 压平、大小写冲突）。
- 01 补丁把 `scripts/setlocalversion` 整段替换成常量 → release 串恒定，**不要**再还原它。
- 01 补丁把 `/proc/config.gz` 的内容指向 `arch/arm64/configs/m2461_stock.config`（IKCONFIG 伪装），该文件必须在树里。
- 原厂 vbmeta 是 RSA-4096 签名的（auth=576/aux=1664）→ **换 kernel 后无论如何都无法通过 AVB 校验**。
  正确做法是**完全不写 AVB**（见 `MEMORY.md §1.4`）；保留 stale vbmeta 或用自有密钥重签都会失败。
- 厂商 vendor 模块 vermagic 不带 `-android14-11-maybe-dirty` 是正常的（CONFIG_MODVERSIONS 有前缀兼容规则），**不用**打放宽 vermagic 的补丁。
- `workflow_dispatch.inputs` ≤10（当前 4）；不要加 push 触发。
- arm64 Image 的稳定魔数在 **偏移 0x38 的 `ARM\x64`**，不是开头 8 字节（开头含随尺寸变化的 `b` 分支指令）。

## 5. 快速命令

```bash
# 取产物（本机）
git -C C:\GKI\work\meizu21-gki fetch --depth 1 origin dist
git -C C:\GKI\work\meizu21-gki worktree add --detach C:\GKI\work\dist21 FETCH_HEAD
```

```bash
# 本地静态校验（不需要编译）
python3 scripts/audit_boot.py C:\MEIZU21_12.7\boot.img          # 原厂镜像，应 PASS
python3 scripts/check_kcfi.py <vmlinux> <clang_bin> device/meizu21/kcfi_expected.txt
```

```bash
# 本地回归：用参考 kernel 重打包，应与可开机参考镜像逐字节相同
python3 scripts/make_boot_zeroavb.py <参考 kernel> /tmp/rt.img \
    --header-template device/meizu21/boot_header_4096.bin --bare
# 期望 sha256 = 3eb02d295ca98a0e4b83689143094524acc55714413312c8601fd3727ef461bb
```

```powershell
# 触发 CI
$h=@{'Accept'='application/vnd.github+json';'User-Agent'='dsh';'Authorization'="Bearer <PAT>"}
$ws=Invoke-RestMethod 'https://api.github.com/repos/FengFaQ/MEIZU21-GKI/actions/workflows' -Headers $h
$id=($ws.workflows | Where-Object {$_.path -like '*meizu21.yml'}).id
Invoke-RestMethod "https://api.github.com/repos/FengFaQ/MEIZU21-GKI/actions/workflows/$id/dispatches" `
  -Method Post -Headers ($h+@{'Content-Type'='application/json'}) -Body (@{ref='main'}|ConvertTo-Json)
```

---

---

## 6. CI 运行记录（逐轮日志分析，2026-11-08）

| run | commit | 失败步骤 | 日志里的真实原因 | 根因 / 修法 |
|---|---|---|---|---|
| #1 | 67e595b | 8. 生成 .config | `TRIM_UNUSED_KSYMS 必须关闭` → exit 1 | 我的断言写错：grep 期望 `^CONFIG_...`，实际行是 `# CONFIG_...`。已在 616d5c0 修 |
| #2 | 6606a42 | 5. 下载 GKI 源码 | `gzip: stdin: unexpected end of file` / `tar: Unexpected EOF`；gki.tar.gz 只有 **119,586,816 B**（应为 226,533,513 B） | **下载被截断**且没有完整性校验。已加 3 次重试 + `gzip -t` + 解压后文件数断言 |
| #3 | 616d5c0 | 11. 编译 Image | **内核编译本身成功**：Image 34,892,288 B、vmlinux 343,501,936 B；失败在 `ls ... Module.symvers: No such file`（exit 2） | 只编 `Image` 时 modpost 产出的是 **`vmlinux.symvers`**，`Module.symvers` 只有编 modules 才有。已改用 `vmlinux.symvers` |
| #4 | 18bc717 | （同 #3） | 同上（该 commit 已修 kCFI 门禁解析，但 symvers 文件名未修） | 见上 |
| #5 | 本轮 | — | — | 含 cache 分支、vmlinux.symvers 修复、FinaSU 实时最新、下载加固 |

> run#3 其实证明了内核可编译：`OBJCOPY arch/arm64/boot/Image` 成功，且 Image 大小与本地预演完全一致（34,892,288 B）。
> run#4 的 kCFI 门禁修复也已在参考 vmlinux 上复测通过（21 MATCH + 3 N/A）。


## 6.1 CI 逐轮记录（2026-11-08，全部在 GitHub Actions）

| run | 卡点 | 日志证据 | 修法 |
|---|---|---|---|
| #1 | config 断言 | `TRIM_UNUSED_KSYMS 必须关闭` | grep 模式补 `# ` |
| #2 | GKI 下载 | `unexpected end of file`；119.6 MB / 应 216 MB | 重试 + `gzip -t` + 文件数断言 |
| #3 | 编译后 `ls` | Image 已生成；`ls Module.symvers` 不存在 | 改 `vmlinux.symvers` |
| #4 | 同上 | — | 被 #6 取代 |
| #6 | **v2 重打包** | 编译成功、门禁 1/2/3 全 PASS；v2 报「骨架无法用 AVB hash rule 校验 descriptor」 | 新增 `--expect-image-size/--expect-salt`（用 device facts 校验布局） |
| #7 | 卡在 git fetch | 仓库 cache 分支 950 MB fetch 卡住近 30 分钟 | 临时 `cancel-in-progress: true` 取消 + 恢复步骤加 `timeout 300` |
| #9 | APK 下载 | `APK size=92`、证书解析失败 | 双通道下载 + `size > 1MB` 断言 |
| #10 | FinaSU latest | `curl -I` HEAD 不 302 → 版本解析失败 | 改 GET 重定向 + API/页面三级兜底 |
| #11 | — | 全部修复版：缓存命中、FinaSU latest、APK、编译、门禁、v1/v2 重打包 | 观察中 |

### run#6 关键实测（内核侧已全部通过）

- Image **34,892,288 B**，banner 与魅族 21 原厂逐字符一致；
- 门禁 1 PASS（`6.1.25-android14-11-maybe-dirty`）；
- 门禁 2 PASS（built 14,688 导出，**missing ∩ NEED = 0**，NEED CRC vs stock 0 mismatch）；
- 门禁 3 **21 MATCH + 3 N/A**（含 12 个 OneMind traceiter）；
- v1 重打包 PASS（vbmeta 与原厂逐字节相同）。

### 编译加速实测

| 路径 | clang 步 | GKI 步 | 合计 |
|---|---|---|---|
| Google 直连 | 214 s | 132 s | 346 s |
| 仓库 cache 分支 | 229 s | 278 s | 507 s（更慢） |
| **Actions 缓存命中** | — | — | **~30–60 s**（run#10 实测步骤 2.5→6 共约 2 分钟） |

## 6.2 FinaSU 升级同步（2026-11-13）

上游 `LyraVoid/FinaSU` 已从 v1.5.0 走到 **v1.6.1**（`14a71d953a09e0d706426a17be0f4b422893ab26`，
2026-11-12 发布，中间还有 v1.5.1/v1.5.2/v1.6.0）。因为流水线是 `finasu_ref=latest`（每次现取），
**不需要改任何 pin 就能跟上**；本次做的是「确认兼容 + 把它变成显式检查」。

### 版本对照实测

| 项 | v1.5.0 | v1.6.1 | 影响 |
|---|---|---|---|
| `kernel/tools/check_susfs_inline.sh` | — | **逐字节相同** | 12 个 inline 入口要求不变 |
| APK 证书 | `0x02cc / 2d89b50a…` | **完全相同** | `KSU_EXPECTED_SIZE/HASH` 无需改（且本来就是动态提取） |
| `kernel/extension/susfs.c` | — | +5 行：新增 `CMD_SUSFS_SET_PRIORITY_UID_FILTER` 分支 | 需内核 `susfs_set_priority_uid_filter()` ✅ 有 |
| `kernel/Kconfig` | — | +11 行 `KSU_SUSFS_PRIORITY_UID_FILTER`（default y） | 需内核 `susfs_parse_priority_app_strings()` ✅ 有（全局符号） |
| `kernel/Kbuild` | — | 探测 `susfs_add_sus_type` → `KSU_SUSFS_HAS_TYPE_REGISTRATION`；preinit runner 加 `-z max-page-size=0x10000` | 我们补丁**没有** `susfs_add_sus_type` → 探测不命中 → 相关调用点编译掉（安全降级） |
| `kernel/feature/selinux_hide.c` | — | +131 行（`security_context_to_sid_live` 等） | 纯 FinaSU 内部改动，不新增内核侧依赖 |

### 新增的兼容闸门

- `scripts/check_finasu_compat.sh`（新文件）：SUSFS Inline 入口自检 + 「FinaSU Kconfig 选项 → 内核侧提供者」
  逐项核对 + `.config` 硬要求检查；`FAIL` 即中止，`WARN` 表示安全降级。
- 流水线新增 **step 8.5**（拿到 `.config` 后复查），并把 `finasu_compat.txt` 纳入产物。

## 7. 编译缓存（2026-11-08 新增，三层）

1. **Actions 缓存（快路径，首选）**：`actions/cache/restore@v4` 恢复 `$RUNNER_TEMP/toolchain` + `common`（约 2.7 GB），
   key = `meizu21-upstream-<GKI_SHA>-<CLANG_ID>-v2`；未命中时构建结束前用 `actions/cache/save@v4` 回填。预期 **~1 分钟**。
2. **仓库 `cache` 分支（持久层）**：`prepare-cache.yml` 下载 GKI(216 MB)+clang(720 MB) → 校验 → 90 MB 分卷 → 推 `cache` 分支；
   `scripts/restore_cache.sh` 逐卷 + 整包 sha256 校验后解压。**实测比 Google 直连慢**（run#6 507 s vs run#3 346 s），
   价值是持久/可移植/不怕下载被截断，不是省时间。
3. **Google 直连（兜底）**：3 次重试 + `gzip -t` + 解压后文件数断言。

- FinaSU 不缓存：每次构建实时取最新 release（`releases/latest` 重定向 + `expanded_assets` HTML，避开匿名 API 限流），
  证书动态提取；dispatch 可填 `finasu_ref` 固定。
- 刷新仓库缓存：改 `cache-trigger.txt` 并 push（build workflow 的 `paths-ignore` 已排除它）。
