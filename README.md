# astrbot_plugin_wol

AstrBot Wake-on-LAN 网络唤醒插件。

## 功能

- 通过聊天命令发送 WOL 魔术包唤醒设备
- 支持多设备管理
- 支持默认广播地址、端口配置
- 支持用户隔离：同一用户私聊和群聊共用设备，不同用户互不可见
- 使用 `平台名:发送者ID` 作为所有者标识，避免不同平台同数字 ID 串数据

## 命令

- `/wol <设备名>`：唤醒指定设备
- `/wol.add <设备名> <MAC> [广播地址] [端口] [描述]`：添加设备
- `/wol.remove <设备名>`：删除设备
- `/wol.list`：列出设备
- `/wol.mac <设备名>`：查看 MAC 地址
- `/wol.help`：查看帮助

示例：

```text
/wol.add nas 00:11:22:33:44:55 192.168.1.255 9 我的NAS
/wol nas
/wol.list
```

## 配置

在 AstrBot 插件配置界面可配置：

- `default_port`：默认 WOL 端口，默认 `9`
- `default_broadcast`：默认广播地址，默认 `255.255.255.255`
- `user_isolation`：用户隔离，默认开启
- `allow_legacy_global_devices`：兼容未绑定用户的旧全局设备，默认关闭
- `show_owner_key`：调试时显示 ownerKey，默认关闭

## 数据

设备数据保存在 AstrBot 插件数据目录：

```text
data/plugin_data/astrbot_plugin_wol/devices.json
```

## 注意

Wake-on-LAN 需要被唤醒设备支持并已在 BIOS/系统网卡设置中启用。
