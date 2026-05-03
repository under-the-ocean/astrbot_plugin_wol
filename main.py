from __future__ import annotations

import asyncio
import json
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform import AstrMessageEvent


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")


@register(
    "astrbot_plugin_wol",
    "under-the-ocean",
    "AstrBot Wake-on-LAN 网络唤醒插件，支持多设备管理与用户隔离",
    "1.0.0",
    "https://github.com/under-the-ocean/koishi-plugin-wol",
)
class WolPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.default_port = self._safe_int(self.config.get("default_port", 9), 9)
        self.default_broadcast = str(self.config.get("default_broadcast", "255.255.255.255") or "255.255.255.255")
        self.user_isolation = bool(self.config.get("user_isolation", True))
        self.allow_legacy_global_devices = bool(self.config.get("allow_legacy_global_devices", False))
        self.show_owner_key = bool(self.config.get("show_owner_key", False))

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_wol"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.devices_file = self.data_dir / "devices.json"
        self._devices: List[Dict[str, Any]] = self._load_devices()

    async def initialize(self):
        logger.info("[WOL] AstrBot WOL 插件已加载")

    async def terminate(self):
        self._save_devices()
        logger.info("[WOL] AstrBot WOL 插件已停止")

    # ================= helpers =================

    def _load_devices(self) -> List[Dict[str, Any]]:
        try:
            if not self.devices_file.exists():
                return []
            data = json.loads(self.devices_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and isinstance(data.get("devices"), list):
                return data["devices"]
        except Exception as e:
            logger.warning(f"[WOL] 读取设备数据失败: {e}")
        return []

    def _save_devices(self):
        try:
            self.devices_file.write_text(
                json.dumps(self._devices, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[WOL] 保存设备数据失败: {e}")

    def _safe_int(self, value: Any, default: int = 0, min_value: Optional[int] = None) -> int:
        try:
            if value is None or value == "":
                return default
            result = int(value)
            if min_value is not None and result < min_value:
                return default
            return result
        except Exception:
            return default

    def _safe_port(self, value: Any, default: Optional[int] = None) -> int:
        default_port = default if default is not None else self.default_port
        port = self._safe_int(value, default_port, min_value=1)
        if port > 65535:
            return default_port
        return port

    def _next_id(self) -> int:
        max_id = 0
        for d in self._devices:
            try:
                max_id = max(max_id, int(d.get("id", 0)))
            except Exception:
                pass
        return max_id + 1

    def _platform_name(self, event: AstrMessageEvent) -> str:
        try:
            name = event.get_platform_name()
            if name:
                return str(name)
        except Exception:
            pass
        return "unknown_platform"

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            uid = event.get_sender_id()
            if uid is not None and str(uid):
                return str(uid)
        except Exception:
            pass
        return "unknown_sender"

    def _owner_key(self, event: AstrMessageEvent) -> str:
        """稳定的设备所有者标识。平台名 + 发送者ID，群号不参与。"""
        return f"{self._platform_name(event)}:{self._sender_id(event)}"

    def _legacy_user_id(self, event: AstrMessageEvent) -> str:
        return self._sender_id(event)

    def _normalize_mac(self, mac: str) -> str:
        return mac.strip().lower().replace("-", ":")

    def _visible_devices(self, event: AstrMessageEvent, name: Optional[str] = None) -> List[Dict[str, Any]]:
        owner_key = self._owner_key(event)
        legacy_user_id = self._legacy_user_id(event)
        name = name.strip() if name else None

        def by_name(d: Dict[str, Any]) -> bool:
            return not name or str(d.get("name", "")) == name

        if not self.user_isolation:
            return [d for d in self._devices if by_name(d)]

        owned = [
            d for d in self._devices
            if by_name(d) and (
                str(d.get("ownerKey", "")) == owner_key
                or (not d.get("ownerKey") and str(d.get("userId", "")) == legacy_user_id)
            )
        ]
        if owned:
            return owned

        if self.allow_legacy_global_devices:
            return [d for d in self._devices if not str(d.get("userId", "")) and by_name(d)]

        return []

    def _find_owned_device_index(self, event: AstrMessageEvent, name: str) -> Optional[int]:
        owner_key = self._owner_key(event)
        legacy_user_id = self._legacy_user_id(event)
        for i, d in enumerate(self._devices):
            if str(d.get("name", "")) != name:
                continue
            if not self.user_isolation:
                return i
            if str(d.get("ownerKey", "")) == owner_key:
                return i
            if not d.get("ownerKey") and str(d.get("userId", "")) == legacy_user_id:
                return i
        return None

    def _create_magic_packet(self, mac: str) -> bytes:
        compact = mac.replace(":", "").replace("-", "")
        if len(compact) != 12:
            raise ValueError("无效的MAC地址格式")
        mac_bytes = bytes.fromhex(compact)
        return b"\xff" * 6 + mac_bytes * 16

    async def _send_wol(self, mac: str, broadcast: str, port: int):
        """发送魔术包。修复：绑定 0.0.0.0:0 避免 Invalid argument。"""
        packet = self._create_magic_packet(mac)
        port = self._safe_port(port, self.default_port)
        if not broadcast or not broadcast.strip():
            broadcast = self.default_broadcast

        def send():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                # 必须先绑定一个本地地址才能设置 SO_BROADCAST
                sock.bind(("0.0.0.0", 0))
                sock.sendto(packet, (broadcast, port))
            finally:
                sock.close()

        await asyncio.to_thread(send)

    def _device_line(self, d: Dict[str, Any]) -> str:
        broadcast = d.get("broadcast") or self.default_broadcast
        port = self._safe_port(d.get("port"), self.default_port)
        line = f"• {d.get('name')}: {d.get('mac')} ({broadcast}:{port})"
        if d.get("description"):
            line += f" - {d.get('description')}"
        if self.show_owner_key and d.get("ownerKey"):
            line += f" [owner: {d.get('ownerKey')}]"
        return line

    # ================= commands =================

    @filter.command("wol")
    async def wake(self, event: AstrMessageEvent, name: str = ""):
        """唤醒指定设备。用法：/wol nas"""
        if not name:
            yield event.plain_result('请指定设备名称，使用 "/wol.list" 查看可用设备')
            return

        devices = self._visible_devices(event, name)
        if not devices:
            yield event.plain_result(f'未找到设备 "{name}"，使用 "/wol.list" 查看可用设备')
            return

        d = devices[0]
        broadcast = d.get("broadcast") or self.default_broadcast
        port = self._safe_port(d.get("port"), self.default_port)
        try:
            await self._send_wol(str(d.get("mac")), str(broadcast), port)
            yield event.plain_result(f'✅ 已发送唤醒信号到 "{d.get("name")}" ({d.get("mac")})')
        except Exception as e:
            logger.error(f"[WOL] 发送WOL信号失败: {e}")
            yield event.plain_result(f"❌ 发送唤醒信号失败: {e}")

    @filter.command("wol.add")
    async def add_device(
        self,
        event: AstrMessageEvent,
        name: str = "",
        mac: str = "",
        broadcast: str = "",
        port: int = 0,
        description: str = "",
    ):
        """添加设备。用法：/wol.add nas 00:11:22:33:44:55 [广播地址] [端口] [描述]"""
        if not name or not mac:
            yield event.plain_result("用法: /wol.add <设备名> <MAC地址> [广播地址] [端口] [描述]\n示例: /wol.add nas 00:11:22:33:44:55 192.168.1.255 9 我的NAS")
            return
        if not MAC_RE.match(mac):
            yield event.plain_result("❌ MAC地址格式错误，正确格式如: 00:11:22:33:44:55 或 00-11-22-33-44-55")
            return

        if self._visible_devices(event, name):
            yield event.plain_result(f'❌ 你的设备列表中已存在 "{name}"，请使用其他名称')
            return

        device = {
            "id": self._next_id(),
            "ownerKey": self._owner_key(event) if self.user_isolation else "",
            "userId": self._legacy_user_id(event) if self.user_isolation else "",
            "platform": self._platform_name(event),
            "name": name,
            "mac": self._normalize_mac(mac),
            "broadcast": broadcast or "",
            "port": self._safe_port(port, self.default_port),
            "description": description or "",
        }
        self._devices.append(device)
        self._save_devices()
        suffix = "，仅你可见" if self.user_isolation else ""
        yield event.plain_result(f'✅ 已添加设备 "{name}" ({device["mac"]}){suffix}')

    @filter.command("wol.remove")
    async def remove_device(self, event: AstrMessageEvent, name: str = ""):
        """删除设备。用法：/wol.remove nas"""
        if not name:
            yield event.plain_result("请指定要删除的设备名称")
            return
        idx = self._find_owned_device_index(event, name)
        if idx is None:
            yield event.plain_result(f'❌ 未找到设备 "{name}"')
            return
        self._devices.pop(idx)
        self._save_devices()
        yield event.plain_result(f'✅ 已删除设备 "{name}"')

    @filter.command("wol.list")
    async def list_devices(self, event: AstrMessageEvent):
        """列出设备。用法：/wol.list"""
        devices = self._visible_devices(event)
        if not devices:
            yield event.plain_result('暂无设备，使用 "/wol.add" 添加设备')
            return
        title = "你的设备:" if self.user_isolation else "已配置的设备:"
        yield event.plain_result(title + "\n" + "\n".join(self._device_line(d) for d in devices))

    @filter.command("wol.mac")
    async def show_mac(self, event: AstrMessageEvent, name: str = ""):
        """查看设备 MAC。用法：/wol.mac nas"""
        if not name:
            yield event.plain_result("请指定设备名称")
            return
        devices = self._visible_devices(event, name)
        if not devices:
            yield event.plain_result(f'❌ 未找到设备 "{name}"')
            return
        d = devices[0]
        yield event.plain_result(f'设备 "{d.get("name")}" 的MAC地址: {d.get("mac")}')

    @filter.command("wol.help")
    async def help(self, event: AstrMessageEvent):
        """查看帮助。用法：/wol.help"""
        text = (
            "AstrBot WOL 网络唤醒插件\n"
            "命令：\n"
            "• /wol <设备名> - 唤醒设备\n"
            "• /wol.add <设备名> <MAC> [广播地址] [端口] [描述] - 添加设备\n"
            "• /wol.remove <设备名> - 删除设备\n"
            "• /wol.list - 列出设备\n"
            "• /wol.mac <设备名> - 查看MAC\n"
            "说明：默认启用用户隔离，同一用户在私聊和群聊可共用自己的设备，不同用户互不可见。\n"
            "示例：/wol.add nas 00:11:22:33:44:55 192.168.1.255 9 我的NAS"
        )
        yield event.plain_result(text)
