# 刷机 SOP · 魅族 21（M2461）

> 前提：BL 已解锁、`fastboot` 可用、已装本 release 附带的 **FinaSU v1.5.0 APK**。
> 铁律：**只刷非活动槽**，保留一个能启动的槽。

---

## 1. 刷前准备

```bash
# 1) 看当前活动槽
adb shell getprop ro.boot.slot_suffix      # 期望 _a 或 _b，记下来（下称 $CUR）

# 2) 确认手机是魅族 21（不是 21 Pro！两机 boot 头完全相同，只能靠系统信息区分）
adb shell getprop ro.product.device        # 必须是 meizu21
adb shell getprop ro.build.display.id      # 期望 Flyme 12.7.0.0A

# 3) 记录当前槽的 boot 备份（强烈建议，虽然一般用不到）
adb reboot bootloader
fastboot getvar current-slot
fastboot fetch boot$CUR boot_stock.img     # 或直接从线刷包取原厂 boot.img

# 4) 校验产物
sha256sum boot.img                          # 与 release 的 sha256sums.txt 比对
```

## 2. 刷入（只刷非活动槽）

```bash
# 假设当前是 _a，就刷 _b
fastboot flash boot_b boot.img
fastboot --set-active=b
fastboot reboot
```

首次开机比较慢（3~5 分钟），**不要慌，也不要反复重启**。

## 3. 验证

```bash
# 1) 起来了吗？版本串对不对？
adb shell cat /proc/version
#   期望：Linux version 6.1.25-android14-11-maybe-dirty (build-user@build-host) ... #1 SMP PREEMPT Wed Oct 28 ...

# 2) 有没有模块加载失败 / kCFI 崩溃
adb shell dmesg | grep -iE 'version magic|CFI failure|Unknown symbol|disagrees about version'
#   期望：空

# 3) Root：装 release 附带的 FinaSU APK → 打开应显示「已内建 / Built-In」，
#    设置里 hook 后端应显示 SUSFS Inline（或 Tracepoint，取决于 workflow 输入）。
adb shell su -c id                          # 授权后应返回 uid=0

# 4) SUSFS 是否生效（管理器里 SUSFS 页应能开）
```

## 4. 起不来 / 异常 → 回退

```bash
# 10 秒回退到原来的槽
adb reboot bootloader
fastboot --set-active=a
fastboot reboot
```

## 5. 故障对照

| 现象 | 最可能原因 | 下一步 |
|---|---|---|
| 卡第一屏 2~3 秒循环重启 | ① release 串不符 ② kCFI 崩溃 ③ 模块 CRC 不符 | `dmesg` 区分：`version magic` / `CFI failure` / `disagrees about version` |
| 直接跳回 Fastboot | AVB0/AVBf 缺失或布局不对 | 跑 `scripts/audit_boot.py boot.img`，应 PASS |
| 能开机但 WiFi/相机/触摸异常 | 对应 vendor 模块没加载 | `dmesg | grep -i unknown symbol`；把缺失符号回填到 `module_kmi_manifest` 复核 |
| 能开机但管理器显示「未安装」 | 装的 APK 与内核里的证书不匹配 | **必须**装 release 附带的那份 FinaSU APK |
| 管理器显示 Built-In 但 hook 异常 | SUSFS Inline 入口没对上 | 用 workflow 输入 `hook_backend=tracepoint` 重编一版 |
| 开机后 `/proc/config.gz` 内容不对 | IKCONFIG 伪装文件没进树 | 检查 `arch/arm64/configs/m2461_stock.config` 是否存在 |

## 6. 首刷后要回填的东西（重要）

把以下信息贴回仓库 issue / 记忆文档，下一轮改门禁：

1. `dmesg | grep -i 'CFI failure'` 里如果出现 `expected type 0x........`，把这个值回填到 `device/meizu21/kcfi_expected.txt`；
2. `dmesg | grep -iE 'Unknown symbol'` 的符号 → 回填 KMI 清单；
3. `/proc/version` 全文（对照原厂）；
4. 管理器首页截图（Built-In / hook 状态 / SUSFS 状态）。

## 7. 二级保险（可选）

如果 v1（原厂 vbmeta 字节）起不来、v2（重算 digest）也起不来，说明 BL 强制校验 AVB。
此时不要继续盲试，先回退，然后：

- 确认 BL 解锁状态（`fastboot getvar unlocked` / `flashing unlock` 已做过？）；
- 考虑 vbmeta 关闭校验（`fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img`）的可行性
  ——⚠️ 这一步会改设备状态，**必须先备份原厂 vbmeta.img**，且魅族机型是否接受需要另行验证。
