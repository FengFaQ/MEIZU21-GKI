# 真机开机失败诊断 SOP（魅族 21 / M2461）

> 目的：**一次刷机就分清**「ABL 拒绝启动」还是「内核 panic」，避免盲试。
> 前提：BL 已解锁；只刷**非活动槽**，随时可 `--set-active` 回退。

---

## 0. 刷之前：记录基线（1 分钟）

```bash
adb shell getprop ro.boot.slot_suffix      # 记下，例：_a
adb shell getprop ro.product.device        # 必须是 meizu21
adb shell getprop ro.build.display.id      # 期望 Flyme 12.7.0.0A
```

---

## 1. 刷入（只刷非活动槽）

```bash
# 假设当前是 _a
fastboot flash boot_b boot.img
fastboot --set-active=b
fastboot reboot
```

**同时准备电脑端抓日志**（关键，决定成败）：

```bash
# 另开一个窗口，刷完立刻执行，抓串口/内核早期日志
adb wait-for-device
adb shell "dmesg" > dmesg.txt 2>/dev/null
```

如果**起不来**，用下面两条通道取崩溃现场：

### 通道 A：pstore（最有用，只要内核跑过一段就会留）
```bash
fastboot reboot bootloader      # 再进 fastboot
fastboot oem pstore read        # 有的机型支持；不支持则跳过
# 或设备能起来后：
adb shell ls -la /sys/fs/pstore/
adb shell cat /sys/fs/pstore/console-ramoops-0 > pstore.txt
```

### 通道 B：last_kmsg / 恢复模式
```bash
# 回退到好槽开机后
adb shell su -c 'cat /proc/last_kmsg' > last_kmsg.txt     # 老内核有这个
adb shell su -c 'cat /sys/fs/pstore/console-ramoops*' > pstore_after.txt
```

---

## 2. 症状对照表（照这个判断，不要猜）

| 现象 | 最可能原因 | 判定证据 |
|---|---|---|
| **完全黑屏，2 秒内直接回 fastboot/bootloader** | **ABL 拒绝**（boot 结构/AVB 校验失败） | 无 logo；`fastboot getvar all` 里 `slot-unbootable` 标志 |
| **亮魅族 logo，停 3–10 秒后黑屏重启**（我们 run#17 的表现） | **内核启动到一半 panic**（kCFI / 模块加载 / 早期 init） | pstore/console-ramoops 有 trace |
| 亮 logo 后无限循环重启（每次 <3 秒） | kCFI trap（`brk #0x8228`）或 `Unknown symbol` | `console-ramoops` 里 `CFI failure` / `Unknown symbol` |
| 卡 logo 不动（不重启） | ramdisk/init 阶段卡死 | 无 panic，需看 `init` 日志 |
| 能开机但无 WiFi/相机 | 对应 vendor 模块未加载 | `dmesg \| grep -i 'Unknown symbol'` |

**我们 run#17 是第二种**（亮 logo 数秒后重启）——这一点很关键：
它说明 **ABL 已经放行了**（否则 logo 都不会出现），所以**问题不在 AVB，
而在内核启动阶段**。这也是我改 AVB 签名方案后仍可能不解决的原因。

---

## 3. 拿到 pstore 后看什么（按顺序 grep）

```bash
grep -iE 'CFI failure|expected type' pstore.txt   # kCFI
grep -iE 'Unknown symbol|disagrees about version' pstore.txt  # KMI/模块
grep -iE 'version magic.*should be|module_layout' pstore.txt  # release 串
grep -iE 'Kernel panic|Unable to handle|BUG:' pstore.txt      # 通用 panic
grep -iE 'init: |Failed to|mount' pstore.txt                  # 早期 init
```

### 三类的处理方向

| grep 到 | 含义 | 处理 |
|---|---|---|
| `expected type 0x.........` | kCFI 哈希不符 | 把该值回填 `device/meizu21/kcfi_expected.txt`，重编 |
| `Unknown symbol xxx` | 内核没导出该符号 | 回填 `module_kmi_manifest.txt`，在补丁里补 `EXPORT_SYMBOL` |
| `version magic ... should be '6.1.25-android14-11-maybe-dirty'` | release 串不符 | 不该发生（门禁 1 已硬校验），若发生说明刷错镜像 |
| `Kernel panic - not syncing:` 后跟栈 | 具体函数崩了 | 把整段发我 |

---

## 4. 二分定位（如果 pstore 拿不到）

用 workflow 输入逐层关闭，每轮只改一个变量：

| 轮次 | 变更 | 目的 |
|---|---|---|
| B1 | `hook_backend=tracepoint` | 排除 SUSFS Inline 直调入口 |
| B2 | 关 `CONFIG_KSU_SUSFS`（需临时改 config） | 排除整个 SUSFS |
| B3 | 关 `CONFIG_KSU`（纯 GKI 内核） | **验证内核本身能否启动** |

> B3 最重要：**纯 GKI（无 KSU/SUSFS）能不能开机**，决定了问题在「内核基础」还是「Root 注入」。

---

## 5. 回退（10 秒）

```bash
fastboot --set-active=a
fastboot reboot
```

---

## 6. 已知未验证项（写在这里免得忘）

1. 魅族 ABL 在 BL 解锁后**是否仍校验 AVB** —— run#17 能出 logo，倾向「不校验或校验通过」。
2. 自编内核的 `Image` 比原厂大 ~1 MB（34,892,288 vs 33,892,864），是否影响启动未知。
3. FinaSU 的 Inline hook 在本机是否正常 —— 只有真机能确认。
