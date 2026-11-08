# 记忆文档 · 魅族 21 GKI

> 只记「为什么」和「踩过什么坑」。想知道「现在在哪」看 `HANDOFF.md`。
> 最后更新：2026-11-08

---

## 1. 铁律（违反了必然开不了机，且现场很难看出来）

### 1.1 GKI 版本不许跳
固定在 `android14-6.1` / `sub_level 25` / `os_patch 2023-10` / tag `r17`。
跳版本会改 `struct zone` / `struct lruvec` 字段偏移，魅族闭源模块（`mz_kshrink_slabd`、`smartswap_zram` 等）
按字节偏移直接访问这些结构 → 野指针 → 开机即崩。

### 1.2 内核 release 串必须逐字符等于原厂
`6.1.25-android14-11-maybe-dirty`

| 位置 | 布局 | 受 release 串影响 |
|---|---|---|
| `vendor_boot` ramdisk | `/lib/modules/*.ko` 平铺 | ❌ |
| `vendor_dlkm` | `/lib/modules/*.ko` 平铺 + `modules.load` | ❌ |
| `system_dlkm` | `/system_dlkm/lib/modules/<release>/` | ✅ |

串不对 → system_dlkm 的模块目录找不到 → 第一阶段 init FATAL → **卡第一屏 2~3 秒后重启**
（和 kCFI 崩溃表现几乎一样，必须 `dmesg` 区分：`version magic ... should be` vs `CFI failure`）。

实现方式：01 补丁把 `scripts/setlocalversion` 整个替换成 `echo "-android14-11-maybe-dirty"`。
**不要还原、不要改成动态生成。**

### 1.3 LTO 必须是 none
`CONFIG_LTO_NONE=y`。`thin` 会改变 kCFI 哈希分布；原厂就是 none。

### 1.4 boot.img 必须带 AVB0 + AVBf，而且 vbmeta 是签名的
魅族 ABL 即使 BL 解锁也读这两块。缺了 → 直接跳 Fastboot。
- `AVBf`（footer）在 `EOF - 64`；`AVB0`（vbmeta）紧接 payload 末尾，**2496 B**。
- **实测**：vbmeta `algorithm_type=2`（SHA256_RSA4096，签名 512 B）、`auth_block_size=576`、
  `aux_block_size=1664`、`descriptors_offset=0`（相对 aux 起点）、`descriptors_size=608`。
  签名覆盖 header + aux block（descriptors 在 aux 里）→ **改 descriptor 就会破坏签名**（没有魅族私钥）。
- 唯一做法：**vbmeta 字节原样保留**，只搬位置 + 改 footer 的 `orig_image_size/vbmeta_offset`。

## 2. 参考树（21 Pro）的配方与复用边界

`C:\GKI\m2481-kernel` 是「手工兼容层」，不是原厂源码 diff。01 补丁（52 文件）含 7 类：

| 类 | 内容 | 对 21 是否适用 |
|---|---|---|
| A | susfs4ksu 上游补丁（25 文件，`#ifdef CONFIG_KSU_SUSFS`） | ✅ 机型无关 |
| B | KernelSU 去 kprobe 化的内核内静态挂钩 | ✅ 机型无关（FinaSU Inline 后端的入口就是它） |
| C | 魅族 vendor hook 复刻（12 个 `android_vh_*` + `EXPORT_TRACEPOINT_SYMBOL_GPL`） | ✅ 两机 KMI 同源（实测 2779 个 NEED CRC 差异 0） |
| D | 厂商模块 NEED 符号补齐 | ✅ 同上 |
| E | kCFI 对齐（`shrink_slab` 取地址等） | ✅ 机型无关 |
| F | 反检测（taint 恒 0、kallsyms 遮蔽、`/proc/config.gz` 伪装、`/bootinfo` 伪造、`check_version` 放行 CRC） | ✅ 大部分通用；config 文件名/内容换成 m2461 的 |
| G | utf8-norm 边界 | ✅ 通用 |

**结论：补丁只做了 2 处 M2461 适配**：注释里的 `M2481`→`M2461/M2481`、`arch/arm64/configs/m2481_stock.config`→`m2461_stock.config`。

## 3. 为什么 Root 用 FinaSU + SUSFS Inline

- 用户指定 FinaSU v1.5.0（tag commit `fbf8e003…`）；
- FinaSU 官方 `kernel/tools/check_susfs_inline.sh` 对「GKI + 01 补丁」**实测返回 0**（它要的 12 个直调入口 `ksu_handle_*` 全在）；
- 内建（`KSU=y`）+ Inline = 无 kprobe、无 text patch、无 sys_enter tracepoint，事件面最小；
- 构建前提：`make headers_install`（preinit runner 需要 `usr/include/linux/unistd.h`）、`KSU_REQUIRE_PREINIT=1`、
  `CONFIG_KALLSYMS_ALL/KPROBES/EXT4_FS/THREAD_INFO_IN_TASK=y`；管理器证书用 `KSU_EXPECTED_SIZE/HASH` 编进内核
  （FinaSU v1.5.0：`0x02cc` / `2d89b50a…448458`）。

## 4. 设备事实（本次实测，权威值见 `device/meizu21/source.txt`）

- 机型：`meizu21` / `MEIZU 21` / M2461 / SM8650（pineapple）/ Flyme 12.7.0.0A。
- 原厂 boot：100,663,296 B，sha256 `8c899eb3…`；kernel 段 33,892,864 B，未压缩 arm64 Image（带 EFI stub）。
- 内核 banner：`6.1.25-android14-11-maybe-dirty (build-user@build-host) … clang 17.0.2 r487747c #1 SMP PREEMPT Wed Oct 28 18:11:17 UTC 2026`。
- vendor_dlkm：304 ko；system_dlkm：57 ko（release 目录名 = 上面那串）；vendor_boot ramdisk：335 ko。
- **KMI**：三处模块并集 **4892 个 NEED 符号**；其中 **3280 个由内核提供**，
  与 21 Pro 原厂 `Module.symvers` **CRC 差异 = 0**（其余是模块间互导，内核不管）。
- **vermagic 怪现象**：vendor 模块 vermagic 是 `6.1.25 SMP preempt mod_unload modversions aarch64`（不带 localversion），
  内核是带 localversion 的串。**这是合法的**：`CONFIG_MODVERSIONS=y` 时 `same_magic()` 允许模块串是内核串的前缀，
  前提是模块带 `__versions`。→ 不需要放宽 vermagic 的补丁。
- **kCFI 期望表**：12 个函数（5 个 device_verified / 4 个 present_not_anchored / 3 个 N/A）+ 12 个 OneMind traceiter
  （继承 21 Pro，类型哈希由「回调签名+编译参数」唯一决定，需在自编译 vmlinux 上复核）。
- **vbmeta 老布局**：`hash_algorithm` 是 32 字节字符串 `"sha256"`；
  descriptor 绝对位置 = `vbmeta + 256 + auth_block_size + descriptors_offset`；
  image_size @ desc+16（u64）、salt @ desc+136、digest @ desc+168。
  `digest = sha256(salt || image[0:image_size])`，且 **salt = kernel 段 sha256**。

## 5. 流水线踩坑（别重踩）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | run 里 0 job / dispatch 422 | `workflow_dispatch.inputs` 超 10 个，GitHub 拒绝注册整个 workflow | 入口只留 4 个 input |
| 2 | push 触发 0 job | 可复用 workflow 的跨 job outputs 在 push 上下文解析失败 | 只留 `workflow_dispatch` |
| 3 | kleaf 不支持 `bazel build target -- VAR=val` | `--` 后被当 target | 本项目改用 **纯 make**，证书走 make 命令行变量 |
| 4 | `autoksyms.h:6:2: expected identifier or '('` | `.raw` 白名单是 CRLF | 本项目不开 TRIM，白名单不进树；`.gitattributes` 强制 LF |
| 5 | `MZ_DEFINE_VH` 造 OneMind 符号 | `__traceiter` 是 `return 0` 空实现 | 检查全绿但功能全失效 —— 本项目不用它造 8 个 OneMind hook |
| 6 | 产物取回被 CRLF 撑大 | `git show` 走 Windows 文本转换 | 走 `dist` 分支 + `git worktree` |
| 7 | 刷两个槽 → 变砖 | 没有可启动槽 | **只刷非活动槽** |
| 8 | 解 vendor_boot 的 21 Pro 模块 | ramdisk 载荷被混淆 | 别试；21 的能解（lz4） |
| 9 | 用 PowerShell 改 UTF-8 中文文件 → 文件损坏 | Windows PowerShell 编码陷阱 | 一律用 `write`/`edit` 工具或 WSL python 改文本 |
| 10 | `fs/stat.c:217: error: call to undeclared function 'susfs_is_data_local_tmp_dir'` | 用了 `susfs4ksu/kernel_patches/` 的旧版 SUSFS 文件，而 01 补丁是按参考树作者改过的版本写的 | 从参考树工作副本取 `common/fs/susfs.c`、`common/include/linux/susfs.h`、`susfs_def.h` |
| 11 | CI 里 `TRIM_UNUSED_KSYMS 必须关闭` 断言误报 | grep 模式写的是 `^CONFIG_... is not set`，实际行是 `# CONFIG_... is not set` | 模式必须带 `# ` |
| 12 | `gzip: stdin: unexpected end of file` / `tar: Unexpected EOF` | Google 归档下载被截断（run#2 只下了 119 MB / 应 216 MB），且没校验完整性 | 重试 3 次 + `gzip -t` + 解压后文件数断言；优先走 cache 分支 |
| 13 | `ls: cannot access 'Module.symvers'` → 构建步骤 exit 2（但 Image 已生成） | 只编 `Image` 时 modpost 产物是 **`vmlinux.symvers`**；`Module.symvers` 只有编 modules 才有 | 门禁 2 / 产物统一用 `vmlinux.symvers` |
| 14 | 自检 `grep susfs_is_current_ksu_domain include/linux/susfs_def.h` 永远失败 | 该符号不在头文件里；而且 `grep && echo` 在 `set -e` 下不会中断，属于「假自检」 | 改成检查真实存在的 `susfs_is_data_local_tmp_dir`，并改成 `|| { exit 1; }` 真断言 |
| 15 | run#6 用仓库 cache 分支后，clang/GKI 两步反而更慢（229 s + 278 s，Google 直连是 214 s + 132 s） | `git fetch --depth 1` 拉 950 MB 分卷 + `git archive | tar` 解 1.5 GB 树的开销，比直接 HTTP 下载 + 解压更大 | 真正的快路径用 **Actions 缓存**（解压好的工具链+源码，key 绑 pin）；仓库 cache 分支只做持久/可移植兜底 |
| 16 | cache 恢复日志 `cache 分支里没有 cache/clang`，实际回退 AOSP | prepare-cache 推分支时 `cp cache/. pkg/` 把 `cache/` 这层目录丢了，文件落在分支根 | 推送时 `cp cache/. pkg/cache/`；恢复脚本按 `cache/<key>/` 取 |
| 17 | v2 重打包报「骨架无法用 AVB hash rule 校验 descriptor，拒绝写 digest」 | CI 模板是 payload 清零的 AVB **骨架**，salt+image 的 hash rule 永远不成立 | 新增 `--expect-image-size/--expect-salt`（来自 device facts），用已知原厂字段校验布局后再重算 |
| 18 | 仓库 cache 分支 git fetch 卡住近 30 分钟 | 950 MB 分支在 Actions 网络上慢且时好时坏；无超时保护 | 恢复步骤加 `timeout 300`，超时按未命中回退 AOSP；真正的快路径用 Actions 缓存 |
| 19 | 管理器 APK 只有 92 字节、证书解析失败 | GitHub 偶发限流/拦截，返回错误页而不是 APK；没有体积校验 | 双通道（API asset → 页面直链）+ 每次断言 `size > 1 MB` + 重试 |
| 20 | `FinaSU 版本解析失败` | `curl -I`（HEAD）请求 `releases/latest` 时 GitHub 不返回 302，`url_effective` 仍是 latest 页 | 改 GET + `-o /dev/null -w %{url_effective}`；API/页面扫描兜底 |



## 6. 明确放弃 / 不做（本轮）

| 项 | 原因 |
|---|---|
| 移植 21 Pro 树的 soter / TEESimulator / 伪装管理器 | 用户选择路线 A：Root 用 FinaSU 官方 |
| 重签 vbmeta | 没有 Meizu 私钥（RSA-4096）；自签 key 只会更糟 |
| 改 `TRIM_UNUSED_KSYMS=y` | 与 KMI 清单语义冲突，且原厂模块只查自己 NEED 的符号 |
| 两槽一起刷 | 刷坏没有回退槽 |
| 在本机 Windows 上编译 | 参考树被拷贝损伤；且用户要求编译放 Actions |

## 7. 待验证（未证实，别当事实用）

1. BL 解锁后是否真的跳过 AVB 校验（决定 v1 能否开机）。
2. 8 个 OneMind traceiter 的 kCFI 期望哈希在 21 上是否与继承值一致（首刷 dmesg 回填）。
3. FinaSU Inline hook 在真机上的状态（管理器显示 Built-In / hook OK？）。
4. `CONFIG_MZ_*`（原厂私有 Kconfig）被 olddefconfig 丢弃后，21 厂商模块行为是否异常。
5. 自编内核与原厂的 `shrink_slab` / `mem_cgroup_id_*` kCFI 前导是否完全一致（门禁 3 会验）。

## 8. 硬约束（用户 2026-11-08 明确要求）

**禁止在本地（WSL/Windows）编译内核 Image。** 理由：WSL 实际可用磁盘只有 **100 GB**（`df` 显示的
900+ GB 是虚拟磁盘，不是配额）。所有 GKI 编译与 boot.img 重打包**只能在 GitHub Actions** 做。
本地只允许：静态分析、脚本单测、解包镜像、跑门禁脚本（不产生 vmlinux/Image）。
历史上本地预演已产生 8.8 GB 构建产物，已全部清理（`/home/gki` 删除）。
