# meizu21（M2461）原厂设备数据 — 来源与可信度

数据全部来自 **Flyme 12.7.0.0A 官方线刷包**（`boot.img` sha256 `8c899eb3b7fcb65eff6bb4afd015f90984ce50d351eceb7eda7ef06bbc4619f5`，96 MiB），
设备实测身份：`ro.product.device=meizu21`、`model=MEIZU 21`、Flyme 项目号 `M2461`；内核 `6.1.25-android14-11-maybe-dirty`。
提取环境：WSL Ubuntu（root），工具为 python3 + 自写 ELF/`__versions` 解析器、`unpack_bootimg.py`、`lz4`、`fsck.erofs`。
生成时间：2026-11-08；临时解包产物在 `C:\GKI\temp\research\01\`。

| 文件 | 内容 | 可信度 |
|---|---|---|
| `source.txt` / `so.txt` / `boot_header.txt` | 机型身份、原厂 boot sha256、release 串、boot v4 头字段 | **最高**：全部从原厂镜像逐字段读出 |
| `stock_kernel.config` | 原厂 boot.img 内 IKCONFIG 解出的 .config（7430 行），删 1 行 jenkins 路径、改 `TRIM_UNUSED_KSYMS`、末尾加 3 行 KSU 配置 | **最高**：除上述 5 处外逐字节等于原厂内核配置 |
| `stock_Module.symvers` | KMI CRC 基准（6501 条 vmlinux 导出，来自 21 Pro 原厂；见同名 README） | **高**：21 侧 3280 个内核导出 NEED 符号与它 CRC 差异 0 |
| `module_kmi_manifest.txt` | vendor_dlkm(304 ko) + system_dlkm(57 ko) + vendor_boot ramdisk(335 ko) 的 `__versions` NEED 表：**4892 符号 / 0 CRC 冲突**，其中 3280 个由内核提供 | **最高**：全部取自模块 ELF `__versions` |
| `vendor_dlkm_modules.txt` / `system_dlkm_modules.txt` | 两分区实际模块清单（304 / 57，system 带 `kernel/` 前缀） | **最高**：目录实测 |
| `kcfi_expected.txt` | 24 个 kCFI typeid 期望值 + 来源标注（`func` 12 + `traceiter` 12） | **混合**：func 组 5 个 device_verified、4 个 present_not_anchored、3 个 N/A；traceiter 组 12 个继承 21 Pro（类型哈希由签名唯一决定，须在自编译 vmlinux 上复核） |
| `avb_template/` | 原厂 boot 的 AVB 骨架（zlib）+ 来源说明 | **最高**：`repack(原厂Image, 骨架)` 与原厂 boot.img **sha256 完全相同**（往返验证） |

**仍需真机 dmesg / 实机回填的项**：
① `kcfi_expected.txt` 的 12 个 traceiter 值必须在自编译 vmlinux 上用 `check_kcfi.py` 复核；
② `module_kmi_manifest.txt` 只证明「原厂模块需要这些符号 + CRC」，我们的内核能否全部导出、模块能否真正加载（尤其厂商模块 vermagic 是 `6.1.25` 而内核是 `6.1.25-android14-11-maybe-dirty` 这一已知但合法的现象）需开机 dmesg 确认；
③ vbmeta 是 RSA-4096 签名的（`auth_block_size=576`、`aux_block_size=1664`），换内核后 descriptor 的 image_size/digest 必然 stale，能否过 bootloader 必须实机验证。
