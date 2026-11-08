# 交接文档 · 魅族 21 GKI

> 给下一个接手的人 / AI。目标：30 分钟内知道「这是什么 / 现在到哪 / 下一步 / 别碰什么」。
> 最后更新：2026-11-08

---

## 0. 一句话

用 **GKI android14-6.1（6.1.25 / os_patch 2023-10）+ 21 Pro 参考树的兼容层补丁 + FinaSU v1.5.0 内建**，
在 GitHub Actions 里编出魅族 21（M2461）的 `Image`，再重打包成带 AVB 的 96 MiB `boot.img`（只替换 boot 分区）。

## 1. 当前状态（2026-11-08）

| 项 | 状态 |
|---|---|
| 设备事实（config / KMI / kCFI / AVB 骨架 / boot 头） | 🟡 正在产出（`device/meizu21/`） |
| 01 补丁按 M2461 适配（2 处 sed） | ✅ 已做，且实测可干净应用到 GKI pin SHA |
| FinaSU SUSFS-Inline 兼容性 | ✅ FinaSU `check_susfs_inline.sh` 对「上游+01」返回 0 |
| AVB 骨架往返验证 | ✅ `repack(原厂Image, 骨架)` 与原厂 boot.img **sha256 完全相同** |
| CI 工作流 | ✅ 已写（`meizu21.yml`），待首跑 |
| 首次 CI 运行 | ❌ 未跑 |
| 真机刷入 | ❌ 未做 |

### 下一步（按顺序）

1. 补齐 `device/meizu21/` 下 5 个数据文件（config / Module.symvers / KMI manifest / kCFI 期望 / AVB 骨架）。
2. 推 `main` → 手动 dispatch `meizu21.yml` → 看 job 日志。
3. 首跑大概率会卡在编译（FinaSU 内建 + 6.1.25 的组合没有现成 CI 记录）。按报错逐条修；本地 WSL 预演（`memory/research/04-local-rehearsal.md`）会先暴露一批。
4. 拿到 Image 后跑门禁 1/2/3，确认 `missing ∩ NEED == ∅`、kCFI 全 MATCH。
5. 产物走 `dist` 分支取回（本机 github.com HTTPS 不通），只刷**非活动槽**。
6. 首刷成功 → 用 dmesg 回填 kCFI 期望值，把门禁 3 转硬。

## 2. 仓库结构

```
.github/workflows/meizu21.yml   唯一入口（手动 dispatch；inputs 4 个）
patches/01-kernel-common.patch  21 Pro 参考树的兼容层，已按 M2461 改名（config 文件名 + 注释）
kernel-add/fs/susfs.c           SUSFS 内核实现（susfs4ksu @27e0a0e）
kernel-add/include/linux/susfs.h, susfs_def.h
device/meizu21/                 设备事实（见 README）
scripts/                        门禁 + 重打包（check_kcfi / check_krcrc_symvers / repack_avb_boot / audit_boot / extract_manager_cert）
docs/FLASH_SOP.md               刷机 / 回退 / 故障诊断
```

## 3. 关键 pin（改任何一个都要重做验证）

| 项 | 值 |
|---|---|
| GKI 源码 | `kernel/common` @ `0d89215310919593150f0ef047f9fd9902e02bc7`（`android14-6.1-2023-10_r17`） |
| 工具链 | `clang-r487747c`（Android clang 17.0.2），AOSP prebuilts 分支 `android14-release` |
| FinaSU | tag `v1.5.0` = `fbf8e003d902c43213d435632a22e4fb1a219aeb`；APK sha256 `6860cc33…b6638` |
| 管理器证书 | `0x02cc` / `2d89b50aaf1db013c2b6738f973f48b62ea21a3a8d7216bfec3521065d448458` |
| 原厂 boot | sha256 `8c899eb3…4619f5`，100,663,296 B |
| 原厂 config | 从原厂 boot.img 的 IKCONFIG 提取（7430 行） |

## 4. 已踩过 / 已验证的坑（详见 `MEMORY.md`）

- 参考树 `C:\GKI\m2481-kernel` **只能读不能编**（Windows 拷贝把 symlink 压平、大小写冲突）。
- 01 补丁把 `scripts/setlocalversion` 整段替换成常量 → release 串恒定，**不要**再还原它。
- 01 补丁把 `/proc/config.gz` 的内容指向 `arch/arm64/configs/m2461_stock.config`（IKCONFIG 伪装），该文件必须在树里。
- 原厂 vbmeta **是 RSA-2048 签名的**（auth=576/aux=1664，descriptor 用老布局 string32）→ 改 digest 会让签名失效，v1 只搬不改。
- 厂商 vendor 模块 vermagic 不带 `-android14-11-maybe-dirty` 是正常的（CONFIG_MODVERSIONS 有前缀兼容规则），**不用**打放宽 vermagic 的补丁。
- `workflow_dispatch.inputs` ≤10（当前 4）；不要加 push 触发。

## 5. 快速命令

```bash
# 取产物（本机）
git -C C:\GKI\work\meizu21-gki fetch --depth 1 origin dist
git -C C:\GKI\work\meizu21-gki worktree add --detach C:\GKI\work\dist21 FETCH_HEAD
```

```bash
# 本地静态校验（不需要编译）
python3 scripts/audit_boot.py C:\MEIZU21_12.7\boot.img          # 应 PASS
python3 scripts/check_kcfi.py <vmlinux> <clang_bin> device/meizu21/kcfi_expected.txt
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

## 7. 编译缓存（2026-11-08 新增）

- `.github/workflows/prepare-cache.yml`：下载 GKI 源码 + clang → 校验 → 分卷（90 MB）→ 推 `cache` 分支。
- `scripts/prepare_cache.sh` / `scripts/restore_cache.sh`：生成 / 恢复缓存（恢复时逐卷 + 整包 sha256 校验）。
- build workflow 的 step 3/5 已改成「先 cache、未命中回退 Google」。
- **FinaSU 故意不缓存**：每次构建实时取最新 release（`finasu_ref: latest`），管理器证书也动态提取；
  需要固定版本时 dispatch 填 `finasu_ref`。
