# stock_Module.symvers 的来源（KMI CRC 基准）

- 文件：`device/meizu21/stock_Module.symvers`
- sha256：`580787ce517ef9d3c3b47cc921bdf8029495aa09e4e6dca3a90ddd4e7e07ca24`
- 行数：6501（全部是 vmlinux 导出符号；不含模块间互导符号）
- 来源：魅族 21 Pro（M2481）原厂内核的 `Module.symvers`（参考树 `C:\GKI\m2481-kernel\baseline\stock_Module.symvers`）。
  魅族 21（M2461）的原厂 `Module.symvers` 不随 ROM 发布、也无法从 Image 可靠反推（两次尝试失败，见
  `memory/research/02-m2481-tree.md` §5）。

## 为什么可以拿 21 Pro 的当 21 的基准（实测证据）

用魅族 21 原厂 12.7 线刷包里的 **361 个 vendor 模块**（vendor_dlkm 304 + system_dlkm 57）+
**335 个 vendor_boot ramdisk 模块**，解析各自 `__versions` 段（modversions CRC 表）：

| 指标 | 值 |
|---|---|
| 模块 NEED 符号（三处并集） | **4892** |
| 其中由内核提供（能在本 symvers 里找到） | **3280** |
| **这些符号的 CRC 与魅族 21 模块期望值的差异** | **0** |
| 两机 .config 差异 | 1 行（`CONFIG_UNUSED_KSYMS_WHITELIST` 的 jenkins 路径） |
| 两机 boot 头 / AVB footer | 逐字节相同 |

即：**两机同一 GKI 源、同一 KMI**。21 侧模块需要的每个内核导出符号，CRC 都与这份 21 Pro 基准逐位相同。

## 将来替换

如果拿到魅族 21 自己的 `Module.symvers`，直接替换本文件，并重跑门禁 2 与真机验证；
同时更新本 README 的 sha256。
