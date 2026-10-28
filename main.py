"""主人的遥控器 — 麻麻说闭嘴，窝就真闭嘴！"""

import json
import os
import re
import time
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

DEFAULT_MAMA_IDS = ["1234567890", "0987654321"]
DEFAULT_SILENCE_SECONDS = 60
DEFAULT_IGNORE_SECONDS = 60
RATE_WINDOW = 60  # 限频统计窗口（秒）


@register("astrbot_plugin_chat_limit", "小七月", "麻麻专属聊天限制插件", "v1.3")
class ControlPlugin(Star):
    """
    控制插件：
    - 说"闭嘴 [秒数]" →窝真的不回复了
    - 说"说话" → 解除闭嘴
    - 说"忽略 QQ号 [秒数]" → 跳过该用户的消息
    - 说"取消忽略 QQ号" → 恢复对该用户的回复
    - 说"限频 QQ号 次数" → 每分钟最多回复该用户几次
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        # 持久化目录使用当前插件名 astrbot_plugin_chat_limit；
        # 旧注册名 astrbot_plugin_control 的目录仅作首次启动的迁移来源（只读）
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_chat_limit")
        os.makedirs(self.data_dir, exist_ok=True)
        self._state_path = os.path.join(self.data_dir, "control_state.json")
        try:
            self._legacy_state_path = os.path.join(
                StarTools.get_data_dir("astrbot_plugin_control"), "control_state.json"
            )
        except Exception:
            self._legacy_state_path = None
        self._ignored: Dict[str, float] = {}
        self._group_silence_until: Dict[str, float] = {}
        self.rate_limits: Dict[str, int] = {}
        self._msg_ts: Dict[str, list] = {}
        self._load_settings()
        self._restore()

    # ── 配置读取 ──────────────────────────────────────

    def _load_settings(self):
        cfg = self.config or {}
        mama = cfg.get("mama_ids") or DEFAULT_MAMA_IDS
        self.mama_ids = {str(x).strip() for x in mama if str(x).strip()}
        self.require_admin = bool(cfg.get("require_admin", False))

        groups = cfg.get("command_settings") or {}

        def grp(name):
            value = groups.get(name) or {}
            return value if isinstance(value, dict) else {}

        shutup = grp("shutup_settings")
        unshutup = grp("unshutup_settings")
        ignore = grp("ignore_settings")
        unignore = grp("unignore_settings")
        ratelimit = grp("ratelimit_settings")
        unratelimit = grp("unratelimit_settings")

        self.cmd_shutup = list(shutup.get("shutup_commands") or ["闭嘴"])
        self.cmd_unshutup = list(unshutup.get("unshutup_commands") or ["说话"])
        self.cmd_ignore = list(ignore.get("ignore_commands") or ["忽略"])
        self.cmd_unignore = list(unignore.get("unignore_commands") or ["取消忽略"])
        self.cmd_ratelimit = list(ratelimit.get("ratelimit_commands") or ["限频"])
        self.cmd_unratelimit = list(unratelimit.get("unratelimit_commands") or ["取消限频"])

        self.dur_shutup = int(shutup.get("default_duration") or DEFAULT_SILENCE_SECONDS)
        self.dur_ignore = int(ignore.get("default_duration") or DEFAULT_IGNORE_SECONDS)

        self.reply_shutup = shutup.get(
            "shutup_reply", "呜…窝闭嘴 {duration} 秒…(´;ω;` )"
        )
        self.reply_unshutup = unshutup.get("unshutup_reply", "好耶～窝复活啦！ヽ(●´∀`●)ﾉ")
        self.reply_ignore = ignore.get(
            "ignore_reply", "遵命麻麻～已忽略用户 {target} {duration} 秒！(｀・ω・´)✧"
        )
        self.reply_unignore = unignore.get(
            "unignore_reply", "好哒～已取消忽略用户 {target}！(๑¯◡¯๑)✧"
        )
        self.reply_ratelimit = ratelimit.get(
            "ratelimit_reply", "好哒～已设置 {target} 每分钟最多 {limit} 条！(๑¯◡¯๑)✧"
        )
        self.reply_unratelimit = unratelimit.get(
            "unratelimit_reply", "好哒～已取消 {target} 的限频！(๑¯◡¯๑)✧"
        )

        # 长指令优先匹配，避免「取消限频」被「限频」先吃掉
        pairs = []
        for name in self.cmd_ratelimit:
            if f"{name}列表" not in self.cmd_unratelimit:
                pairs.append((f"{name}列表", "ratelimit_list"))
        for name in self.cmd_unratelimit:
            pairs.append((name, "unratelimit"))
        for name in self.cmd_ratelimit:
            pairs.append((name, "ratelimit"))
        for name in self.cmd_unignore:
            pairs.append((name, "unignore"))
        for name in self.cmd_ignore:
            pairs.append((name, "ignore"))
        for name in self.cmd_unshutup:
            pairs.append((name, "unshutup"))
        for name in self.cmd_shutup:
            pairs.append((name, "shutup"))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._commands = pairs

    # ── 持久化 ────────────────────────────────────────

    def _restore(self):
        try:
            # 新目录没有状态文件时，回退读取旧注册名目录并迁移过来
            source_path = self._state_path
            migrated = False
            if not os.path.isfile(source_path):
                if self._legacy_state_path and os.path.isfile(self._legacy_state_path):
                    source_path = self._legacy_state_path
                    migrated = True
            if os.path.isfile(source_path):
                with open(source_path) as f:
                    d = json.load(f)
                self._group_silence_until = d.get("group_silence_until", {}) or {}
                self._ignored = d.get("ignored", {}) or {}
                raw_limits = d.get("rate_limits", {}) or {}
                # 0 是合法值：表示完全屏蔽（每分钟最多 0 条）；只丢弃负数/非法值
                self.rate_limits = {}
                dropped = []
                for k, v in raw_limits.items():
                    try:
                        n = int(v)
                    except (TypeError, ValueError):
                        dropped.append(str(k))
                        continue
                    if n >= 0:
                        self.rate_limits[str(k)] = n
                    else:
                        dropped.append(str(k))
                if dropped:
                    logger.info(
                        f"[Control] 已清理无效限频记录 {len(dropped)} 条: {', '.join(dropped)}"
                    )
                if migrated:
                    logger.info(
                        f"[Control] 已迁移旧状态文件到 {self._state_path}"
                    )
                if migrated or dropped:
                    self._flush()
        except Exception as e:
            logger.warning(f"[Control] 加载状态失败: {e}")

    def _flush(self):
        now = time.time()
        self._ignored = {k: v for k, v in self._ignored.items() if v > now}
        self._group_silence_until = {
            k: v for k, v in self._group_silence_until.items() if v > now
        }
        try:
            with open(self._state_path, "w") as f:
                json.dump(
                    {
                        "group_silence_until": self._group_silence_until,
                        "ignored": self._ignored,
                        "rate_limits": self.rate_limits,
                    },
                    f,
                )
        except Exception as e:
            logger.warning(f"[Control] 保存状态失败: {e}")

    # ── 状态查询 ──────────────────────────────────────

    def _scope_key(self, event: AstrMessageEvent) -> str:
        """按会话隔离闭嘴状态：群聊用群号，私聊用发送者。"""
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id()}"

    def _muted(self, event: AstrMessageEvent) -> bool:
        scope = self._scope_key(event)
        silence_until = self._group_silence_until.get(scope, 0.0)
        if silence_until <= 0:
            return False
        if time.time() >= silence_until:
            self._group_silence_until.pop(scope, None)
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
        if str(event.get_sender_id()) not in self.mama_ids:
            return False
        if self.require_admin:
            try:
                return bool(event.is_admin())
            except Exception:
                return True
        return True

    def _rate_limited(
        self, event: AstrMessageEvent, uid: str, record: bool = True
    ) -> bool:
        """按 RATE_WINDOW 统计条数；record=False 只判断不计数，同一条消息只计一次。"""
        limit = self.rate_limits.get(uid)
        if limit is None or limit < 0:
            return False
        if limit == 0:
            # 0 条 = 完全屏蔽，一条都不放过
            return True
        now = time.time()
        ts_list = [t for t in self._msg_ts.get(uid, []) if now - t < RATE_WINDOW]
        self._msg_ts[uid] = ts_list
        if len(ts_list) >= limit:
            return True
        if record and not event.get_extra("_chat_limit_counted", False):
            ts_list.append(now)
            event.set_extra("_chat_limit_counted", True)
        return False

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        if seconds >= 60:
            minutes, rest = divmod(seconds, 60)
            return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分钟"
        return f"{seconds} 秒"

    @staticmethod
    def _extract_numbers(text: str) -> list:
        return re.findall(r"\d+", text or "")

    @staticmethod
    def _clean_text(raw: str) -> str:
        """剥离命令前缀与唤醒词，兼容 / 、% 、! 、七月！ 等写法。"""
        text = (raw or "").strip()
        stripped = True
        while stripped and text:
            stripped = False
            for wake in ("七月！", "七月!", "小七月！", "小七月!"):
                if text.startswith(wake):
                    text = text[len(wake):].strip()
                    stripped = True
                    break
            match = re.match(r"^[^\w\u4e00-\u9fff]+", text)
            if match:
                text = text[match.end():].strip()
                stripped = True
        return text

    @staticmethod
    def _match_command(text: str, name: str):
        """只有“命令本身”或“命令后接空白/数字参数”才算命中，避免自然语句误触发。"""
        if not name:
            return None
        if text == name:
            return ""
        if not text.startswith(name):
            return None
        rest = text[len(name):]
        if not rest:
            return ""
        first = rest[0]
        if (
            first.isspace()
            or first.isdigit()
            or first in ":：,，、;；"
            or first in "吧啊呀啦哦喔哈嘛呢了掉下个"
        ):
            return rest.strip()
        return None

    def _render(self, template: str, **kwargs) -> str:
        try:
            return template.format(**kwargs)
        except Exception:
            return template

    # ── 双路拦截：LLM 请求 + yield 结果 ──────────────

    @filter.on_llm_request()
    async def _block_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 开工前拦截 — 窝的独家方案 ✨"""
        uid = str(event.get_sender_id())

        if self._muted(event):
            logger.info("[Control] 🛑 闭嘴中，LLM 请求已拦截")
            event.should_call_llm(False)
            req.contexts.clear()
            return

        if not self._is_mama(event) and self._skipped(uid):
            logger.info(f"[Control] 🛑 已忽略用户 {uid}，LLM 请求已拦截")
            event.should_call_llm(False)
            req.contexts.clear()
            return

        # 限频只在这里计数，避免和结果钩子重复计数
        if not self._is_mama(event) and self._rate_limited(event, uid, record=True):
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

        uid = str(event.get_sender_id())

        if self._muted(event):
            logger.info("[Control] 🛑 闭嘴中，yield 结果已拦截")
            result.chain = []
            return

        if not self._is_mama(event) and self._skipped(uid):
            logger.info(f"[Control] 🛑 已忽略用户 {uid}，yield 结果已拦截")
            result.chain = []
            return

        # 这里只判断，不重复计数
        if not self._is_mama(event) and self._rate_limited(event, uid, record=False):
            logger.info(f"[Control] 🛑 用户 {uid} 触发限频，yield 结果已拦截")
            result.chain = []

    # ── 指令总入口 ────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10001)
    async def _dispatch(self, event: AstrMessageEvent):
        if not self._is_mama(event):
            return

        text = self._clean_text(event.message_str or "")
        if not text:
            return

        for name, action in self._commands:
            args = self._match_command(text, name)
            if args is None:
                continue
            handler = getattr(self, f"_do_{action}", None)
            if handler is None:
                continue
            async for msg in handler(event, args):
                if msg is not None:
                    yield msg
            return

    # ── 指令：闭嘴 ────────────────────────────────────

    async def _do_shutup(self, event: AstrMessageEvent, args: str):
        nums = self._extract_numbers(args)
        sec = int(nums[0]) if nums else self.dur_shutup
        self._group_silence_until[self._scope_key(event)] = time.time() + sec
        self._flush()
        event.stop_event()
        yield event.plain_result(
            self._render(self.reply_shutup, duration=self._fmt_duration(sec))
        )

    # ── 指令：说话 ────────────────────────────────────

    async def _do_unshutup(self, event: AstrMessageEvent, args: str):
        if self._muted(event):
            self._group_silence_until.pop(self._scope_key(event), None)
            self._flush()
            yield event.plain_result(self.reply_unshutup)
        else:
            yield event.plain_result("窝本来就在说话呀～麻麻你是不是记错啦？(｀・ω・´)")
        event.stop_event()

    # ── 指令：忽略 ────────────────────────────────────

    async def _do_ignore(self, event: AstrMessageEvent, args: str):
        nums = self._extract_numbers(args)
        if not nums:
            yield event.plain_result("麻麻～格式是「忽略 QQ号 秒数」哦！(｀・ω・´)")
            event.stop_event()
            return
        target = nums[0]
        sec = int(nums[1]) if len(nums) > 1 else self.dur_ignore
        self._ignored[target] = time.time() + sec
        self._flush()
        yield event.plain_result(
            self._render(
                self.reply_ignore, target=target, duration=self._fmt_duration(sec)
            )
        )
        event.stop_event()

    # ── 指令：取消忽略 ────────────────────────────────

    async def _do_unignore(self, event: AstrMessageEvent, args: str):
        nums = self._extract_numbers(args)
        if not nums:
            yield event.plain_result("麻麻～格式是「取消忽略 QQ号」哦！(｀・ω・´)")
            event.stop_event()
            return
        target = nums[0]
        if target in self._ignored:
            del self._ignored[target]
            self._flush()
            yield event.plain_result(self._render(self.reply_unignore, target=target))
        else:
            yield event.plain_result(f"麻麻～{target} 本来就没被忽略呀！(｀・ω・´)")
        event.stop_event()

    # ── 指令：限频 ────────────────────────────────────

    async def _do_ratelimit(self, event: AstrMessageEvent, args: str):
        nums = self._extract_numbers(args)
        if len(nums) < 2:
            yield event.plain_result("麻麻～格式是「限频 QQ号 次数」哦！(｀・ω・´)")
            event.stop_event()
            return
        target, limit = nums[0], int(nums[1])
        if limit < 0:
            yield event.plain_result("麻麻～次数不能是负数哦！填 0 就是完全屏蔽～(｀・ω・´)")
            event.stop_event()
            return
        self.rate_limits[target] = limit
        self._msg_ts.pop(target, None)
        self._flush()
        yield event.plain_result(
            self._render(self.reply_ratelimit, target=target, limit=limit)
        )
        event.stop_event()

    # ── 指令：取消限频 ────────────────────────────────

    async def _do_unratelimit(self, event: AstrMessageEvent, args: str):
        nums = self._extract_numbers(args)
        if not nums:
            yield event.plain_result("麻麻～格式是「取消限频 QQ号」哦！(｀・ω・´)")
            event.stop_event()
            return
        target = nums[0]
        if target in self.rate_limits:
            del self.rate_limits[target]
            self._msg_ts.pop(target, None)
            self._flush()
            yield event.plain_result(
                self._render(self.reply_unratelimit, target=target)
            )
        else:
            yield event.plain_result(f"麻麻～{target} 本来就没被限频哦！(｀・ω・´)")
        event.stop_event()

    # ── 指令：限频列表 ────────────────────────────────

    async def _do_ratelimit_list(self, event: AstrMessageEvent, args: str):
        if not self.rate_limits:
            yield event.plain_result("目前没有设置任何限频哦～(｀・ω・´)")
        else:
            lines = "\n".join(
                (
                    f"{qq} → 完全屏蔽（0 条/{RATE_WINDOW // 60}分钟）"
                    if n == 0
                    else f"{qq} → 最多 {n} 条/{RATE_WINDOW // 60}分钟"
                )
                for qq, n in self.rate_limits.items()
            )
            yield event.plain_result(f"📋 限频列表：\n{lines}")
        event.stop_event()
