# MEIZU 21 GKI（M2461）

为 **魅族 21（M2461，骁龙 8 Gen 3 / SM8650）** 构建可刷入的 GKI 内核：
**GKI android14-6.1 / 6.1.25 / os_patch 2023-10 + FinaSU v1.5.0（内核内建，SUSFS Inline 后端）**，
只替换 `boot` 分区，系统仍是原厂 Flyme 12.7。

**所有编译与 boot.img 重打包都在 GitHub Actions 完成**（本仓库只放「配方」，不放 8.7 GB 源码）。

---

## 一句话流程

```
AOSP kernel/common @ 0d8921531  (android14-6.1-2023-10_r17)
  + patches/01-kernel-common.patch        (参考树 21 Pro 的兼容层：SUSFS / vendor hooks / KMI / kCFI / 反检测)
  + kernel-add/{fs/susfs.c, include/linux/susfs*.h}
  + FinaSU v1.5.0 kernel (SUSFS Inline 后端, 内建 KSU=y)
  + device/meizu21/stock_kernel.config    (魅族 21 原厂 IKCONFIG)
  + clang-r487747c (Android clang 17.0.2)
  ──> arch/arm64/boot/Image
  ──> repack 进 device/meizu21/avb_template（原厂 Flyme 12.7 boot 骨架，AVB0+AVBf 布局）
  ──> boot.img (v1 原厂 vbmeta 字节 / v2 重算 hash descriptor)
```

## 硬约束（违反必然开不了机）

| 约束 | 值 | 为什么 |
|---|---|---|
| 内核 release 串 | **逐字符** `6.1.25-android14-11-maybe-dirty` | `system_dlkm/lib/modules/<release>/` 是目录名，串错 = 57 个 GKI 模块找不到 = 卡第一屏 |
| GKI 源码 | pin `0d89215310919593150f0ef047f9fd9902e02bc7` | 补丁按此 SHA 生成；跳版本会改 `struct zone`/`struct lruvec` 偏移 |
| LTO | `none` | 原厂就是 none；thin 会改 kCFI 哈希分布 |
| kCFI | `CONFIG_CFI_CLANG=y` + 24 个期望哈希门禁 | 厂商模块在间接调用点写死类型哈希，不符 = `brk #0x8228` panic |
| boot AVB | 96 MiB，AVB0 在 payload 末尾、AVBf 在 EOF-64 | 魅族 ABL 即使 BL 解锁也会读这两块 |
| 刷机 | **只刷非活动槽** | 刷坏还能 `fastboot --set-active=a` 退回 |

## 刷入（只刷非活动槽）

```bash
adb shell getprop ro.boot.slot_suffix     # 假设 _a
fastboot flash boot_b boot.img            # 用 release 里的 v1
fastboot --set-active=b
# 起不来：fastboot --set-active=a
```

验证：`adb shell dmesg | grep -iE 'version magic|CFI failure|Unknown symbol'`；
Root 用 **release 附带的那份 FinaSU APK**（内核里编的是它的证书哈希，装别的版本会显示“未安装”）。

详见 [`docs/FLASH_SOP.md`](docs/FLASH_SOP.md)。

## 目录

| 路径 | 内容 |
|---|---|
| `.github/workflows/meizu21.yml` | 唯一入口：手动 dispatch，编译 + 门禁 + 重打包 + 发布 |
| `patches/01-kernel-common.patch` | 参考树（21 Pro）的兼容层补丁，已按 M2461 改名适配 |
| `kernel-add/` | 补丁之外需要单独放置的新文件（SUSFS 内核实现） |
| `device/meizu21/` | 设备事实：原厂 config / KMI 基准 / kCFI 期望 / AVB 骨架 / boot 头 |
| `scripts/` | 门禁与重打包脚本（本地可直接跑） |
| `docs/FLASH_SOP.md` | 刷机 / 回退 / 故障诊断 SOP |
| `HANDOFF.md` | 交接文档：现在在哪、下一步 |
| `MEMORY.md` | 记忆文档：为什么这么做、踩过哪些坑 |

## 触发

两种：`workflow_dispatch`（手动，可选 hook 后端/严格 kCFI/发布开关）与 **push 到 `main`**（会自动跑一次默认配置）。
> 之所以保留 push 触发：本机 fine-grained PAT 没有 `Actions: write`，无法用 API 触发 dispatch；
> push 触发时 `inputs` 为空，workflow 用 `inputs.x || '默认值'` 兜底。

`workflow_dispatch.inputs` 上限 10 个（GitHub 硬限制）。`workflow_dispatch.inputs` 上限 10 个（GitHub 硬限制，超限会**拒绝注册整个 workflow**，表现为 run 里 0 job）。

```powershell
$h=@{'Accept'='application/vnd.github+json';'User-Agent'='dsh';'Authorization'="Bearer <PAT>"}
Invoke-RestMethod "https://api.github.com/repos/FengFaQ/MEIZU21-GKI/actions/workflows/<ID>/dispatches" `
  -Method Post -Headers $h -Body (@{ref='main'}|ConvertTo-Json)
```

产物取回走 `dist` 分支（本机 `github.com` HTTPS 不通）：

```powershell
git -C C:\GKI\work\meizu21-gki fetch --depth 1 origin dist
git -C C:\GKI\work\meizu21-gki worktree add --detach C:\GKI\work\dist21 FETCH_HEAD
```

## 许可

本仓库的补丁/脚本按各自上游许可；内核源码遵循 GPL-2.0。FinaSU 内核部分 GPL-2.0、其余 GPL-3.0。
