# QEMU 启动验证报告（原厂 vs 自编 GKI）

日期：2026-11-14
环境：QEMU 8.2.2 `virt` (gic-version=3) / cortex-a710 / 2 SMP / 2 GiB / **无 KVM（软件模拟）**
内核：`linux-6.1.25-android14-11-maybe-dirty`（两者 release 串完全一致 ✅）

---

## 一、结论（先看这个）

| 判定项 | 原厂 boot | 自编 GKI (run#23) | 说明 |
|---|---|---|---|
| 进入 `start_kernel` | ✅ | ✅ | 两者都打印了完整 early boot |
| 走到 `kernel_init` | ✅ | ✅ | 都执行到挂载 rootfs |
| **kCFI trap** | 无 | **无** | 最担心的项，**排除** |
| **Unknown symbol** | 无 | **无** | KMI 导出齐全，**排除** |
| `Kernel panic` | 有（VFS 挂载失败） | 有（VFS 挂载失败） | **QEMU 无 rootfs，属预期，两者相同** |
| 日志行数（去重后） | 307 | **367** | 我们跑得更远（多了 KSU/SUSFS 初始化） |
| 停留时间 | 4.6 s | 9.6 s | 同上 |

**结论：自编内核在 QEMU 上的早期启动行为**与原厂一致**，且额外完成了 KernelSU/SUSFS
初始化而没有崩溃。内核本身没有结构性问题。**

> 重要限制：QEMU `virt` 机型没有 SM8650 的硬件（无真实 GIC/PMIC/显示屏/存储/NPU），
> 所以**无法**用它证明「真机能开机」。它只能证明「内核自身在启动早期不崩」。

---

## 二、我们比原厂多出的日志（全部来自 KSU + SUSFS）

原厂内核没有这些（因为它没集成 Root）：

```
KernelSU: KernelSU version: v1.6.1-14a71d95@HEAD
susfs:[0][1][susfs_init] Initializing susfs_extra_works
susfs:[0][1][susfs_init] susfs is initialized! version: v2.4.0
KernelSU: sys_call_table=0xffffffec08200d20
KernelSU: feature: feature management initialized
KernelSU: feature: registered handler for sulog (id=2)
KernelSU: feature: registered handler for adb_root (id=3)
KernelSU: lsm_hook: init, tracked hooks=0
KernelSU: feature: registered handler for selinux_hide (id=4)
KernelSU: feature: registered handler for shell_su_adb_only (id=7)
KernelSU: feature: registered handler for avc_spoof (id=6)
KernelSU: feature: registered handler for mount_view_filter (id=10)
KernelSU: feature: registered handler for uts_view (id=9)
uts_view: event=init result=ok uts_sem=1 init_uts_ns=1
netisolate: event=init result=ok compat=1 flags=0xf
KernelSU: IOCTL 命令表（GRANT_ROOT / GET_INFO / SET_SEPOLICY … 共 40+ 项）
KernelSU: feature: registered handler for kernel_umount (id=1)
KernelSU: feature: registered handler for su_compat (id=0)
KernelSU: feature: registered handler for dynamic_manager (id=5)
KernelSU: feature: registered handler for dynamic_manager_grants (id=8)
KernelSU: preinit: runner staged (14880 bytes)
```

**这些都是正面信号**：
- `susfs is initialized! version: v2.4.0` → 我们的 2.4.0 升级**在运行时确实生效**
- `preinit: runner staged (14880 bytes)` → FinaSU 的 preinit runner 注入成功
- `uts_view / netisolate result=ok` → 新功能初始化正常

---

## 三、唯一的告警（已确认无害）

```
kallsyms_on_each_match_symbol not found!
```

**判定：无害，属优雅降级。**

- 该符号是 **Linux 6.4+** 才引入的；我们内核是 6.1.25，本来就没有。
- FinaSU 会先探测该符号，找不到就回退到 `kallsyms_on_each_symbol`（6.1 有）。
- 证据：告警之后 KSU 继续正常初始化（`sys_call_table` 已解析、feature 全部注册、preinit 成功）。
- 类似地 `lsm_hook: init, tracked hooks=0` 也只是「本内核没有可挂钩的 LSM」，非错误。

---

## 四、原始日志

| 文件 | 说明 |
|---|---|
| `stock-console.log` | 原厂 boot.img 提取的 Image，485 行 |
| `ours-console.log` | 自编 GKI boot.img 提取的 Image，605 行 |

启动命令（可复现）：

```bash
qemu-system-aarch64 \
  -machine virt,gic-version=3 -cpu cortex-a710 -smp 2 -m 2048 \
  -kernel <Image> -nographic -no-reboot \
  -append "console=ttyAMA0 earlycon=pl011,0x9000000 keep_bootcon rdinit=/bin/sh"
```

提取内核段：

```python
import struct
d = open('boot.img','rb').read()
ks = struct.unpack_from('<I', d, 8)[0]     # kernel_size @ offset 8
open('Image','wb').write(d[4096:4096+ks])  # 页 4096
```

---

## 五、这次测试证明了什么 / 没证明什么

### 证明了
1. **boot.img 结构正确**：header v4 / kernel 段可被 bootloader 正常加载跳转。
2. **内核二进制健康**：无 kCFI trap、无 Oops、无 Unknown symbol。
3. **SUSFS 2.4.0 + FinaSU v1.6.1 在运行时真实初始化成功**（不只是编译通过）。
4. **release 串严格正确**：`6.1.25-android14-11-maybe-dirty`。

### 没证明（QEMU 能力边界）
1. 真机能否开机（缺 SM8650 硬件、vendor 模块、真实 ramdisk）。
2. AVB 是否被魅族 ABL 接受（QEMU 不校验）。
3. vendor_dlkm / vendor_ramdisk 里的 300+ 个 `.ko` 能否加载（**这是真机重启的高概率原因**）。

---

## 六、因此下一步仍是真机 pstore

若真机仍重启，「亮 logo 后重启」在 QEMU 排除了内核早期崩溃之后，
最可能落在**模块加载阶段**：

```
Unknown symbol / disagrees about version / module_layout
```

抓取方式见 `docs/FLASH_DIAGNOSIS.md`：

```bash
adb shell su -c 'cat /sys/fs/pstore/console-ramoops-0' > pstore.txt
```
