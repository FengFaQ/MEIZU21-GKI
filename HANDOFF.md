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

## 6. CI 运行记录（最新在最后）

| run | 结果 | 卡点 | 修法 |
|---|---|---|---|
| #1 | ❌ | 步 8「TRIM 断言」——`grep '^CONFIG_TRIM_UNUSED_KSYMS is not set'` 少了 `# ` 前缀 | 补 `# ` |
| #2 | ❌ | 同上（修脚本的 commit 已含 gate 新脚本，但断言 bug 未修） | 同 #1 |
| #3 | ⏳ | 用参考树改过的 SUSFS 三文件 + 修好的断言 | 观察中 |

> run#1 已证明：clang-r487747c 下载、GKI 源码拉取、01 补丁应用、FinaSU 集成、olddefconfig 全部 OK；
> 最终 `.config` 里 `CONFIG_KSU=y / KSU_SUSFS=y / KSU_HOOK_SUSFS_INLINE=y / KSU_DYNAMIC_MANAGER=y` 全部正确。

### 已知坑（本轮新增）

1. **SUSFS 三文件必须用参考树工作副本版**（`common/fs/susfs.c`、`common/include/linux/susfs*.h`），
   不能用 `susfs4ksu/kernel_patches/` 里的旧版：作者改过（加了 `susfs_is_data_local_tmp_dir()`），
   旧版会在 `fs/stat.c:217` 报 undeclared。
2. 断言里 grep `.config` 的 `is not set` 行必须带 `# ` 前缀。
