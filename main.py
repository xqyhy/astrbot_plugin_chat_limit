"""bot的聊天限制插件"""

import json
import os
import re
import time
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

MAMA_IDS = {"", ""}
DEFAULT_SILENCE_SECONDS = 60
DEFAULT_IGNORE_SECONDS = 60


@register("astrbot_plugin_control", "小七月", "bot聊天限制插件", "v1.2")
class ControlPlugin(Star):
    """
    控制插件：
    - 说"闭嘴 [秒数]" → 本狐真的不回复了
    - 说"说话" → 解除闭嘴
    - 说"忽略 QQ号 [秒数]" → 跳过该用户的消息
    - 说"取消忽略 QQ号" → 恢复对该用户的回复
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_control")
        os.makedirs(self.data_dir, exist_ok=True)
        self._state_path = os.path.join(self.data_dir, "control_state.json")
        self._ignored: Dict[str, float] = {}
        self._silence_until: float = 0.0
        self.rate_limits: Dict[str, int] = {}  # QQ号 → 每分钟限制条数
        self._msg_ts: Dict[str, list] = {}  # QQ号 → [时间戳列表]
        self._restore()

    # ── 持久化 ────────────────────────────────────────

    def _restore(self):
        try:
            if os.path.isfile(self._state_path):
                with open(self._state_path) as f:
                    d = json.load(f)
                self._silence_until = d.get("silence_until", 0.0)
                self._ignored = d.get("ignored", {})
                self.rate_limits = d.get("rate_limits", {})
        except Exception as e:
            logger.warning(f"[Control] 加载状态失败: {e}")

    def _flush(self):
        # 顺手清理过期项
        now = time.time()
        self._ignored = {k: v for k, v in self._ignored.items() if v > now}
        try:
            with open(self._state_path, "w") as f:
                json.dump({"silence_until": self._silence_until, "ignored": self._ignored, "rate_limits": self.rate_limits}, f)
        except Exception as e:
            logger.warning(f"[Control] 保存状态失败: {e}")

    # ── 状态查询 ──────────────────────────────────────

    def _muted(self) -> bool:
        if self._silence_until <= 0:
            return False
        if time.time() >= self._silence_until:
            self._silence_until = 0.0
            self._flush()
            return False
        return True

    def _skipped(self, uid: str) -> bool:
        expire = self._ignored.get(uid)
        if expire is None:
            return False
        if time.time() >= expire:
            del self._ignored[uid]
            self._flush()
            return False
        return True

    def _is_mama(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in MAMA_IDS

    def _rate_limited(self, uid: str) -> bool:
        limit = self.rate_limits.get(uid)
        if limit is None:
            return False
        now = time.time()
        ts_list = self._msg_ts.get(uid, [])
        ts_list[:] = [t for t in ts_list if now - t < 60]
        if len(ts_list) >= limit:
            return True
        ts_list.append(now)
        return False

    def _pick_number(self, text: str, fallback: int) -> int:
        m = re.search(r"(\d+)\s*(?:秒|s)?$", text)
        return int(m.group(1)) if m else fallback

    # ── 双路拦截：LLM 请求 + yield 结果 ──────────────

    @filter.on_llm_request()
    async def _block_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 开工前拦截 — 本狐的独家方案 ✨"""
        uid = event.get_sender_id()

        # 全闭嘴 → 直接掐断 LLM 请求
        if self._muted():
            logger.info(f"[Control] 🛑 闭嘴中，LLM 请求已拦截")
            event.should_call_llm(False)
            req.contexts.clear()
            return

        # 用户被忽略 → 同样掐断
        if not self._is_mama(event) and self._skipped(uid):
            logger.info(f"[Control] 🛑 已忽略用户 {uid}，LLM 请求已拦截")
            event.should_call_llm(False)
            req.contexts.clear()
            return

        # 限频 → 掐断
        if not self._is_mama(event) and self._rate_limited(uid):
            logger.info(f"[Control] 🛑 用户 {uid} 触发限频，LLM 请求已拦截")
            event.should_call_llm(False)
            req.contexts.clear()
            return

    @filter.on_decorating_result()
    async def _block_result(self, event: AstrMessageEvent) -> None:
        """在 yield 结果发出去前拦截 — 补上被动消息回复的漏洞 ✨"""
        result = event.get_result()
        if not result or not result.chain:
            return

        uid = event.get_sender_id()

        # 全闭嘴 → 清空结果链
        if self._muted():
            logger.info(f"[Control] 🛑 闭嘴中，yield 结果已拦截")
            result.chain = []
            return

        # 用户被忽略 → 同样清空
        if not self._is_mama(event) and self._skipped(uid):
            logger.info(f"[Control] 🛑 已忽略用户 {uid}，yield 结果已拦截")
            result.chain = []
            return

        # 限频 → 清空
        if not self._is_mama(event) and self._rate_limited(uid):
            logger.info(f"[Control] 🛑 用户 {uid} 触发限频，yield 结果已拦截")
            result.chain = []
            return

    # ── 指令：闭嘴 ────────────────────────────────────

    @filter.command("闭嘴", priority=10001)
    async def cmd_shutup(self, event: AstrMessageEvent, duration: str = ""):
        if not self._is_mama(event):
            return
        sec = self._pick_number(duration, DEFAULT_SILENCE_SECONDS)
        self._silence_until = time.time() + sec
        self._flush()
        yield event.plain_result(
            f"呜…本狐闭嘴 {sec//60} 分钟…(´;ω;`)" if sec >= 60
            else f"呜呜…本狐闭嘴 {sec} 秒…(´;ω;｀)"
        )
        event.stop_event()

    # ── 指令：说话 ────────────────────────────────────

    @filter.command("说话", priority=10001)
    async def cmd_speak(self, event: AstrMessageEvent):
        if not self._is_mama(event):
            return
        if self._muted():
            self._silence_until = 0.0
            self._flush()
            yield event.plain_result("好耶～本狐复活啦！ヽ(●´∀`●)ﾉ")
            event.stop_event()
        else:
            yield event.plain_result("本狐本来就在说话呀～主人你是不是记错啦？(｀・ω・´)")
            event.stop_event()

    # ── 指令：忽略 ────────────────────────────────────

    @filter.command("忽略", priority=10001)
    async def cmd_ignore(self, event: AstrMessageEvent, target: str = "", duration: str = ""):
        if not self._is_mama(event):
            return
        if not target.isdigit():
            yield event.plain_result("主人～格式是「忽略 QQ号 秒数」哦！(｀・ω・´)")
            event.stop_event()
            return
        sec = self._pick_number(duration, DEFAULT_IGNORE_SECONDS)
        self._ignored[target] = time.time() + sec
        self._flush()
        yield event.plain_result(
            f"遵命主人～已忽略 {target} {sec//60} 分钟！(｀・ω・´)✧" if sec >= 60
            else f"遵命主人～已忽略 {target} {sec} 秒！(｀・ω・´)✧"
        )
        event.stop_event()

    # ── 指令：取消忽略 ────────────────────────────────

    @filter.command("取消忽略", priority=10001)
    async def cmd_unignore(self, event: AstrMessageEvent, target: str = ""):
        if not self._is_mama(event):
            return
        if not target.isdigit():
            yield event.plain_result("主人～格式是「取消忽略 QQ号」哦！(｀・ω・´)")
            event.stop_event()
            return
        if target in self._ignored:
            del self._ignored[target]
            self._flush()
            yield event.plain_result(f"好哒～已取消忽略 {target}！(๑¯◡¯๑)✧")
        else:
            yield event.plain_result(f"主人～{target} 本来就没被忽略呀！(｀・ω・´)")
        event.stop_event()

    # ── 指令：限频 ────────────────────────────────────

    @filter.command("限频", priority=10001)
    async def cmd_ratelimit(self, event: AstrMessageEvent, target: str = "", limit: str = ""):
        if not self._is_mama(event):
            return
        text = event.get_plain_text()
        parts = text.split()
        if len(parts) < 3:
            yield event.plain_result("主人～格式是「限频 QQ号 次数」哦！(｀・ω・´)")
            event.stop_event()
            return
        m = re.search(r"(\d+)", parts[1])
        if not m:
            yield event.plain_result("主人～QQ号格式不对哦！(｀・ω・´)")
            event.stop_event()
            return
        qq_id = m.group(1)
        if not parts[2].isdigit():
            yield event.plain_result("麻麻～次数要是数字哦！(｀・ω・´)")
            event.stop_event()
            return
        self.rate_limits[qq_id] = int(parts[2])
        self._flush()
        yield event.plain_result(f"好哒～已设置 {qq_id} 每分钟最多 {parts[2]} 条！(๑¯◡¯๑)✧")
        event.stop_event()

    # ── 指令：取消限频 ────────────────────────────────

    @filter.command("取消限频", priority=10001)
    async def cmd_unratelimit(self, event: AstrMessageEvent, target: str = ""):
        if not self._is_mama(event):
            return
        m = re.search(r"(\d+)", event.get_plain_text())
        qq_id = m.group(1) if m else ""
        if not qq_id:
            yield event.plain_result("主人～格式是「取消限频 QQ号」哦！(｀・ω・´)")
            event.stop_event()
            return
        if qq_id in self.rate_limits:
            del self.rate_limits[qq_id]
            self._flush()
            yield event.plain_result(f"好哒～已取消 {qq_id} 的限频！(๑¯◡¯๑)✧")
        else:
            yield event.plain_result(f"主人～{qq_id} 本来就没被限频哦！(｀・ω・´)")
        event.stop_event()

    # ── 指令：限频列表 ────────────────────────────────

    @filter.command("限频列表", priority=10001)
    async def cmd_ratelimit_list(self, event: AstrMessageEvent):
        if not self._is_mama(event):
            return
        if not self.rate_limits:
            yield event.plain_result("目前没有设置任何限频哦～(｀・ω・´)")
        else:
            lines = "\n".join(f"{qq} → 最多 {n} 条/分钟" for qq, n in self.rate_limits.items())
            yield event.plain_result(f"📋 限频列表：\n{lines}")
        event.stop_event()
