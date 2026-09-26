"""龟龟乐园：群聊签到与龟龟币。

本文件是插件与 AstrBot 的唯一接触面：指令注册、自动签到、白名单外的 LLM 拦截、
导入页的 Web API。业务逻辑都在 ``funhub/`` 包里，那些模块不依赖 astrbot，可以脱离
宿主单独测试。

指令需要唤醒（AstrBot 全局 ``wake_prefix``，默认 ``/``）：``/签到``、``/面板``、
``/排行``；``@机器人 签到`` 等也算唤醒，群里直接发裸 ``签到`` 不会触发。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .funhub import messages, roulette, webapi
from .funhub.access import (
    at_mentions,
    identity_from_event,
    in_whitelist,
    is_woken,
    looks_like_command,
    message_text,
    near_miss_command,
    parse_command,
    parse_ranking_args,
    whitelist_only,
)
from .funhub.checkin import CheckinService, auto_checkin_reply
from .funhub.commands import (
    ALL_COMMAND_NAMES,
    COMMAND_ALIASES,
    DUEL_WAKE_WORDS,
    DUEL_WILDCARD_NAMES,
    canonical_command,
)
from .funhub.config import (
    CONFIG_SCHEMA_VERSION,
    load_settings,
    migrate_superseded_defaults,
)
from .funhub.db import Database
from .funhub.drunk import DrunkSource
from .funhub.duel import DuelService
from .funhub.platform_api import group_platform_for
from .funhub.players import PlayerIdentity
from .funhub.roulette import RouletteService

try:  # AstrBot 4.11+ 的 Web API 外壳；缺失时只影响导入页。
    from astrbot.api.web import PluginUploadFile, error_response, json_response, request

    WEB_API_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于宿主版本
    PluginUploadFile = error_response = json_response = request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

try:  # 指令参数注解：命令行过滤器只按注解识别 GreedyStr。
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - 版本差异时的降级

    class GreedyStr(str):  # type: ignore[no-redef]
        """降级实现：拿不到宿主的标记类型时，至少保证参数能传进来。"""


PLUGIN_NAME = "astrbot_plugin_funhub"
PLUGIN_VERSION = "0.1.0"

#: 事件上的标记：同一条消息只处理一次指令（见 ``_claim``）。
FUNHUB_CLAIM_KEY = "funhub_command"

#: 数据库 meta 里的键：配置升级只跑一次的标记。
CONFIG_META_KEY = "config_schema_version"


@register(PLUGIN_NAME, "QuanWenG", "群聊签到与龟龟币", PLUGIN_VERSION)
class FunHubPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.settings = load_settings(self.config)
        for warning in self.settings.warnings:
            logger.warning(f"[{PLUGIN_NAME}] 配置有问题：{warning}")

        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.database = Database(self.data_dir / "funhub.db")
        self.service = CheckinService(self.database, self.settings)
        self.duel_service = DuelService(self.database, self.settings)
        #: 醉酒状态来自别的插件（默认 astrbot_plugin_repeater 的喝酒功能）
        self.drunk_source = DrunkSource(lambda: self.context.get_all_stars())
        self.roulette_service = RouletteService(
            self.database,
            self.settings,
            drunk=self.drunk_source,
        )
        if self.service.zone_warning:
            logger.warning(f"[{PLUGIN_NAME}] {self.service.zone_warning}")

        self._ready = False
        self._ready_lock = asyncio.Lock()
        #: 已经提示过"不在白名单"的群，避免刷日志
        self._whitelist_warned: set[str] = set()
        self._register_web_apis()

    # ------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        """建库建表，并把待导入目录准备好。"""
        webapi.import_dir(self.data_dir)
        if await self._ensure_ready():
            await self._upgrade_legacy_config()
            whitelist = self.settings.access.group_whitelist
            logger.info(
                f"[{PLUGIN_NAME}] 龟龟乐园已就绪，数据目录 {self.data_dir}",
            )
            logger.info(
                f"[{PLUGIN_NAME}] 指令："
                f"{' '.join('/' + name for name in COMMAND_ALIASES)}"
                f"（别名见 README；开头多打「龟龟」也认）",
            )
            if whitelist:
                logger.info(f"[{PLUGIN_NAME}] 白名单群：{'、'.join(whitelist)}")
            else:
                # "插件没反应"最常见的原因就是这一条，必须显眼
                logger.warning(
                    f"[{PLUGIN_NAME}] 白名单为空 —— 任何群都不会响应！"
                    f"请在插件配置 access.group_whitelist 里填群号"
                    f"（也支持 aiocqhttp:群号 这种写法）",
                )
            logger.info(
                f"[{PLUGIN_NAME}] 自动签到："
                f"{'开启' if self.settings.checkin.auto_checkin else '关闭'}"
                f"（出声策略 {self.settings.checkin.auto_checkin_reply}），"
                f"换日时刻 {self.settings.checkin.day_reset_hour}:00 "
                f"{self.settings.checkin.timezone}",
            )
            duel = self.settings.duel
            logger.info(
                f"[{PLUGIN_NAME}] 决斗："
                f"{'开启' if duel.enabled else '关闭'}"
                f"（赔付上限 {duel.stake_max}，禁言 {duel.mute_seconds} 秒，按余额尽所能赔付；"
                f"倍率 存款每多 {duel.rich_step_coins} 币 +{duel.rich_step_percent}%"
                f"（封顶 +{duel.rich_cap_percent}%），管理员再 +{duel.admin_percent / 100:g} 倍）",
            )
            roulette = self.settings.roulette
            logger.info(
                f"[{PLUGIN_NAME}] 轮盘："
                f"{'开启' if roulette.enabled else '关闭'}"
                f"（{roulette.chambers} 个弹槽，空枪 +{roulette.safe_reward}"
                f"（每枪再加 {roulette.safe_reward_step}），"
                f"中弹 -{roulette.hit_penalty}（{roulette.share_percent}% 赔给开过枪的人）"
                f"并禁言 {roulette.mute_seconds} 秒；"
                f"子弹那一枪 {roulette.misfire_percent:g}% 炸膛，开枪的人白拿 "
                f"+{roulette.misfire_reward}）",
            )
            self._log_drunk_link()

    def _log_drunk_link(self) -> None:
        """醉酒联动到底接上没有 —— 补一枪的反噬默认只在喝醉时才会翻车。"""
        sources = self.drunk_source.instances()
        drunk_only = self.settings.roulette.judgment_backfire_drunk_only
        if not drunk_only:
            logger.info(
                f"[{PLUGIN_NAME}] 补一枪：反噬不看醉酒状态（judgment_backfire_drunk_only=false）",
            )
        elif sources:
            logger.info(
                f"[{PLUGIN_NAME}] 补一枪：反噬只在醉酒时触发，"
                f"醉酒状态接的是 {'、'.join(name for name, _ in sources)}；"
                f"醉酒走火 {self.settings.roulette.drunk_extra_percent:g}% 概率"
                f"（最多连带 {self.settings.roulette.drunk_extra_max} 人）",
            )
        else:
            logger.warning(
                f"[{PLUGIN_NAME}] 没找到喝酒插件（astrbot_plugin_repeater），"
                f"醉酒联动不可用 —— 补一枪永远不会反噬。"
                f"想让它在清醒时也可能翻车，把 roulette.judgment_backfire_drunk_only 关掉",
            )

    async def terminate(self) -> None:
        await self.database.close()

    async def _upgrade_legacy_config(self) -> None:
        """把"还停在旧默认值"的配置项升到新默认值，只跑一次。

        AstrBot 的插件配置是存过盘的：改了默认值，用户那份旧值不会自己跟着走
        （"第一枪怎么还是 100"就是这么来的）。这里只动恰好等于旧默认值的项，
        用户自己调过的数值一律保留，并且把结果写进数据库 meta 保证不再重复。
        """
        stored = await self.database.get_meta(CONFIG_META_KEY)
        if stored == str(CONFIG_SCHEMA_VERSION):
            return
        upgraded, changes = migrate_superseded_defaults(self.config)
        if changes:
            self.settings = load_settings(upgraded)
            self.service.settings = self.settings
            self.duel_service.settings = self.settings
            self.roulette_service.settings = self.settings
            logger.warning(
                f"[{PLUGIN_NAME}] 配置里还留着旧版本的默认值，已按新规则升级"
                f"（你自己改过的项不受影响）：{'、'.join(changes)}",
            )
        await self.database.set_meta(CONFIG_META_KEY, str(CONFIG_SCHEMA_VERSION))

    async def _ensure_ready(self) -> bool:
        """惰性建库。失败不抛出，让群内指令回一句可读的错误。"""
        if self._ready:
            return True
        async with self._ready_lock:
            if self._ready:
                return True
            try:
                await self.database.connect()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 打开数据库失败")
                return False
            self._ready = True
        return True

    # ------------------------------------------------------------- 群内指令

    def _claim(self, event: AstrMessageEvent, command: str) -> bool:
        """同一条消息只处理一次指令。

        ``@机器人 签到`` 可能同时命中两条路径：``CommandFilter``（平台已把 @ 从文本
        里去掉时）和下面的 ``mentioned_command``（@ 标记还留在文本里时）。谁先跑到
        谁处理，另一条看到标记就让路，避免回两次。
        """
        if event.get_extra(FUNHUB_CLAIM_KEY) is not None:
            return False
        event.set_extra(FUNHUB_CLAIM_KEY, command)
        return True

    def _warn_outside_whitelist(self, event: AstrMessageEvent) -> None:
        """白名单外的指令静默不回复，但必须留痕 —— 否则看起来就像"插件没反应"。

        每个群只提示一次，避免刷日志。
        """
        group_id = str(event.get_group_id() or "")
        if group_id in self._whitelist_warned:
            return
        self._whitelist_warned.add(group_id)
        current = self.settings.access.group_whitelist
        logger.warning(
            f"[{PLUGIN_NAME}] 收到指令但群不在白名单，已忽略：group={group_id or '(私聊)'} "
            f"platform={event.get_platform_id()}；当前白名单="
            f"{'、'.join(current) if current else '（空）'}",
        )

    def _whitelist_ok(self, event: AstrMessageEvent) -> bool:
        if in_whitelist(self.settings, event):
            return True
        self._warn_outside_whitelist(event)
        return False

    @filter.command("签到", alias=COMMAND_ALIASES["签到"])
    async def check_in(self, event: AstrMessageEvent):
        """每日签到领取龟龟币，连签满 7 / 30 天有周期奖励。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "签到"):
            return
        async for result in self.run_check_in(event):
            yield result

    @filter.command("面板", alias=COMMAND_ALIASES["面板"])
    async def profile(self, event: AstrMessageEvent):
        """查看自己的等级、连签、龟龟币与本群排名。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "面板"):
            return
        async for result in self.run_profile(event):
            yield result

    @filter.command("排行", alias=COMMAND_ALIASES["排行"])
    async def ranking(self, event: AstrMessageEvent, args: GreedyStr):
        """本群排行。``/排行`` 按等级，``/排行 币`` 按龟龟币，``/排行 @某人`` 查名次。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "排行"):
            return
        async for result in self.run_ranking(event, args):
            yield result

    @filter.command("决斗", alias=COMMAND_ALIASES["决斗"])
    async def duel(self, event: AstrMessageEvent, args: GreedyStr):
        """和 @到的人掷一把定胜负：输的人掉龟龟币，机器人是管理员时还会被禁言。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "决斗"):
            return
        async for result in self.run_duel(event, args):
            yield result

    @filter.command("轮盘", alias=COMMAND_ALIASES["轮盘"])
    async def roulette(self, event: AstrMessageEvent):
        """开一局龟龟轮盘：6 个弹槽 1 颗子弹，空枪发币、中弹扣币并禁言。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "轮盘"):
            return
        async for result in self.run_roulette(event):
            yield result

    @filter.command("开枪", alias=COMMAND_ALIASES["开枪"])
    async def shoot(self, event: AstrMessageEvent):
        """轮盘里扣一次扳机。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "开枪"):
            return
        async for result in self.run_shoot(event):
            yield result

    @filter.command("救一下", alias=COMMAND_ALIASES["救一下"])
    async def rescue(self, event: AstrMessageEvent, args: GreedyStr):
        """把中弹被禁言的人从禁言里捞出来。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "救一下"):
            return
        async for result in self.run_rescue(event, args):
            yield result

    @filter.command("补一枪", alias=COMMAND_ALIASES["补一枪"])
    async def judgment(self, event: AstrMessageEvent, args: GreedyStr):
        """给中弹被禁言的人再补一枪（追加禁言，有概率卡壳或反噬）。"""
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, "补一枪"):
            return
        async for result in self.run_judgment(event, args):
            yield result

    # ------------------------------------------------------------- @ 唤醒通道

    def _dispatchable_command(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """这条消息会被当成哪条指令处理（``None`` = 不处理）。

        两个来源：

        * ``CommandFilter`` 拿原始文本匹配，而 QQ 官方机器人会把 ``<@机器人id>`` 留在
          文本里、还常常不给 ``At`` 组件，指令名因此不在开头也匹配不上；
        * 老插件的 ``艾斯比`` / ``啥比`` 本身就是唤起词，出现在句子里任何位置都算
          （「艾斯比吧你」），而且只要 @ 了对手就能开打，不需要再 @ 机器人。

        自动签到靠它判断"这条消息会不会有别的回复"，所以判定必须和实际派发一致。
        """
        parsed = parse_command(event, ALL_COMMAND_NAMES, wildcard=DUEL_WILDCARD_NAMES)
        if parsed is None:
            return None
        command, args = parsed
        canonical = canonical_command(command)
        if not is_woken(event):
            # 便捷唤起词：不要求唤醒机器人，但必须 @ 到对手，免得闲聊里误触发
            if canonical != "决斗" or command not in DUEL_WAKE_WORDS:
                return None
            if not at_mentions(event):
                return None
        return canonical, args

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def mentioned_command(self, event: AstrMessageEvent):
        """``@机器人 指令`` 与老插件「便捷唤起词」的通道。"""
        decision = self._dispatchable_command(event)
        if decision is None:
            # 打错指令名时别一声不吭 —— "没反应"会被当成插件坏了
            missed = near_miss_command(event, ALL_COMMAND_NAMES)
            if missed is None:
                return
            if not self._whitelist_ok(event):
                return
            if not self._claim(event, "提示"):
                return
            logger.info(
                f"[{PLUGIN_NAME}] 指令名对不上，已提示 group={event.get_group_id()} "
                f"user={event.get_sender_id()} text={message_text(event)!r} → "
                f"{canonical_command(missed)}",
            )
            yield event.plain_result(messages.command_hint(canonical_command(missed)))
            event.stop_event()
            return
        canonical, args = decision
        if not self._whitelist_ok(event):
            return
        if not self._claim(event, canonical):
            return
        logger.info(
            f"[{PLUGIN_NAME}] @唤醒指令 group={event.get_group_id()} "
            f"user={event.get_sender_id()} → {canonical}",
        )
        runner = {
            "签到": self.run_check_in,
            "面板": self.run_profile,
            "排行": self.run_ranking,
            "决斗": self.run_duel,
            "轮盘": self.run_roulette,
            "开枪": self.run_shoot,
            "救一下": self.run_rescue,
            "补一枪": self.run_judgment,
        }.get(canonical)
        if runner is None:  # pragma: no cover - 指令表与分支保持同步
            return
        if canonical in {"排行", "决斗", "救一下", "补一枪"}:
            async for result in runner(event, args):
                yield result
        else:
            async for result in runner(event):
                yield result

    # ------------------------------------------------------------- 指令实现

    async def run_check_in(self, event: AstrMessageEvent):
        if not await self._ensure_ready():
            yield event.plain_result(messages.CHECKIN_FAILED)
            event.stop_event()
            return
        try:
            outcome = await self.service.check_in(
                identity_from_event(event),
                source="command",
            )
            yield event.plain_result(self.service.render_checkin(outcome))
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 签到失败")
            yield event.plain_result(messages.CHECKIN_FAILED)
        event.stop_event()

    async def run_profile(self, event: AstrMessageEvent):
        if not await self._ensure_ready():
            yield event.plain_result(messages.CHECKIN_FAILED)
            event.stop_event()
            return
        try:
            view = await self.service.profile(identity_from_event(event))
            yield event.plain_result(self.service.render_profile(view))
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 读取面板失败")
            yield event.plain_result(messages.CHECKIN_FAILED)
        event.stop_event()

    async def run_ranking(self, event: AstrMessageEvent, args: str):
        if not await self._ensure_ready():
            yield event.plain_result(messages.CHECKIN_FAILED)
            event.stop_event()
            return
        try:
            identity = identity_from_event(event)
            by_coins, target_id = parse_ranking_args(event, args)
            if target_id:
                yield event.plain_result(
                    await self._render_target_rank(identity, target_id),
                )
            else:
                entries = await self.service.ranking(identity, by_coins=by_coins)
                yield event.plain_result(
                    self.service.render_ranking(entries, by_coins=by_coins),
                )
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 读取排行失败")
            yield event.plain_result(messages.CHECKIN_FAILED)
        event.stop_event()

    async def run_duel(self, event: AstrMessageEvent, args: str):
        del args  # 决斗不看策略文本，只认 @ 到的人
        if not self.settings.duel.enabled:
            yield event.plain_result("决斗没开")
            event.stop_event()
            return
        if not await self._ensure_ready():
            yield event.plain_result(messages.DUEL_FAILED)
            event.stop_event()
            return
        identity = identity_from_event(event)
        target_id, target_name = self._duel_target(event, identity)
        if not target_id:
            yield event.plain_result("用法：/决斗 @某人")
            event.stop_event()
            return
        try:
            result = await self.duel_service.duel(
                identity,
                target_identity_for(identity, target_id, target_name),
                platform=group_platform_for(event, zone=self.service.zone),
                zone=self.service.zone,
            )
            yield event.plain_result(self.duel_service.render(result))
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 决斗失败")
            yield event.plain_result(messages.DUEL_FAILED)
        event.stop_event()

    def _duel_target(self, event: AstrMessageEvent, identity: PlayerIdentity) -> tuple[str, str]:
        """取被 @ 的决斗对象（含群名片）。

        优先级：**先挑不是 ``self_id`` 的人**。QQ 官方机器人上 ``self_id`` 常常是那个
        被 @ 的对手（老插件为此专门写了用例），所以只有当他也是唯一一个 @ 时才回退到
        他 —— 否则 ``@机器人 艾斯比 @对手`` 会把机器人自己当成对手。
        """
        del identity
        mentions = at_mentions(event)
        if not mentions:
            return "", ""
        self_id = str(event.get_self_id() or "")
        for user_id, name in mentions:
            if user_id != self_id:
                return user_id, name
        return mentions[0]

    def _mention_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        """取被 @ 的人（排除机器人自己），用于"对别人下手"的指令。"""
        mentions = at_mentions(event)
        return mentions[0] if mentions else ("", "")

    # ------------------------------------------------------------- 轮盘

    async def run_roulette(self, event: AstrMessageEvent):
        """开一局轮盘。"""
        if not self.settings.roulette.enabled:
            yield event.plain_result("轮盘没开")
            event.stop_event()
            return
        if not await self._ensure_ready():
            yield event.plain_result(messages.ROULETTE_FAILED)
            event.stop_event()
            return
        try:
            outcome = await self.roulette_service.start(identity_from_event(event))
            if outcome.busy:
                yield event.plain_result(messages.ROULETTE_BUSY)
            else:
                yield event.plain_result(
                    messages.roulette_start_card(
                        chambers=outcome.chambers,
                        safe_reward=outcome.safe_reward,
                        safe_reward_step=outcome.safe_reward_step,
                        hit_penalty=outcome.hit_penalty,
                        misfire_percent=outcome.misfire_percent,
                        misfire_reward=outcome.misfire_reward,
                        mute_seconds=outcome.mute_seconds,
                        share_percent=outcome.share_percent,
                    ),
                )
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 开局轮盘失败")
            yield event.plain_result(messages.ROULETTE_FAILED)
        event.stop_event()

    async def run_shoot(self, event: AstrMessageEvent):
        """扣一次扳机。"""
        if not await self._ensure_ready():
            yield event.plain_result(messages.ROULETTE_FAILED)
            event.stop_event()
            return
        identity = identity_from_event(event)
        try:
            outcome = await self.roulette_service.shoot(
                identity,
                platform=group_platform_for(event, zone=self.service.zone),
            )
            yield event.plain_result(self._render_shot(identity, outcome))
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 轮盘开枪失败")
            yield event.plain_result(messages.ROULETTE_FAILED)
        event.stop_event()

    def _render_shot(self, identity: PlayerIdentity, outcome) -> str:
        name = identity.nickname or identity.user_id
        if outcome.kind == roulette.SHOT_NO_ROUND:
            return messages.NO_ROULETTE
        if outcome.kind == roulette.SHOT_MISFIRE:
            logger.info(
                f"[{PLUGIN_NAME}] 轮盘炸膛 group={identity.group_id} "
                f"user={identity.user_id} reward=+{outcome.reward}",
            )
            return messages.roulette_misfire_card(
                name=name,
                shots=outcome.shots,
                chambers=outcome.chambers,
                reward=outcome.reward,
            )
        if outcome.kind == roulette.SHOT_MISS:
            return messages.roulette_miss_card(
                shots=outcome.shots,
                chambers=outcome.chambers,
                reward=outcome.reward,
            )
        logger.info(
            f"[{PLUGIN_NAME}] 轮盘中弹 group={identity.group_id} user={identity.user_id} "
            f"paid={outcome.paid} muted={outcome.muted} drunk={outcome.drunk} "
            f"victims={[item.user_id for item in outcome.victims]}",
        )
        if len(outcome.victims) > 1:
            return messages.roulette_drunk_card(
                victims=[(item.name, item.paid) for item in outcome.victims],
                shots=outcome.shots,
                chambers=outcome.chambers,
                penalty=outcome.penalty,
                mute_seconds=outcome.mute_seconds,
                any_muted=any(item.muted for item in outcome.victims),
                any_blocked=any(item.mute_blocked for item in outcome.victims),
                share_each=outcome.share_each,
                share_names=outcome.share_names,
            )
        return messages.roulette_hit_card(
            name=name,
            shots=outcome.shots,
            chambers=outcome.chambers,
            paid=outcome.paid,
            penalty=outcome.penalty,
            muted=outcome.muted,
            mute_seconds=outcome.mute_seconds,
            mute_blocked=outcome.mute_blocked,
            share_each=outcome.share_each,
            sharers=outcome.sharers,
            share_names=outcome.share_names,
        )

    async def run_rescue(self, event: AstrMessageEvent, args: str):
        """把中弹被禁言的人捞出来。"""
        del args
        identity = identity_from_event(event)
        target_id, target_name = self._mention_target(event)
        if not target_id:
            yield event.plain_result("用法：/救一下 @某人")
            event.stop_event()
            return
        if not await self._ensure_ready():
            yield event.plain_result(messages.ROULETTE_FAILED)
            event.stop_event()
            return
        try:
            outcome = await self.roulette_service.rescue(
                identity,
                target_id,
                platform=group_platform_for(event, zone=self.service.zone),
            )
            yield event.plain_result(
                messages.roulette_rescue_card(
                    kind=outcome.kind,
                    name=target_name or target_id,
                    seconds=outcome.seconds,
                ),
            )
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 轮盘救援失败")
            yield event.plain_result(messages.ROULETTE_FAILED)
        event.stop_event()

    async def run_judgment(self, event: AstrMessageEvent, args: str):
        """``/补一枪``：给中弹被禁言的人追加一次禁言。

        照搬 Pallas-Bot 的 judgment，去掉踢人模式与醉酒联动：只能补本局中弹、
        且还在禁言里的人；不带 @ 时对名单里所有人补。
        """
        del args
        if not await self._ensure_ready():
            yield event.plain_result(messages.ROULETTE_FAILED)
            event.stop_event()
            return
        identity = identity_from_event(event)
        targets = tuple(at_mentions(event))
        try:
            outcome = await self.roulette_service.judgment(
                identity,
                targets,
                platform=group_platform_for(event, zone=self.service.zone),
            )
            logger.info(
                f"[{PLUGIN_NAME}] 补一枪 group={identity.group_id} "
                f"user={identity.user_id} kind={outcome.kind} names={list(outcome.names)}",
            )
            yield event.plain_result(
                messages.roulette_judgment_card(
                    kind=outcome.kind,
                    names=outcome.names,
                    seconds=outcome.seconds,
                    actor_name=outcome.actor_name,
                ),
            )
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 补一枪失败")
            yield event.plain_result(messages.ROULETTE_FAILED)
        event.stop_event()

    async def _render_target_rank(
        self,
        identity: PlayerIdentity,
        target_id: str,
    ) -> str:
        target = target_identity_for(identity, target_id)
        player = await self.service.players.get(target)
        if player is None:
            return messages.no_record(target_id)
        # 与面板一致：名次按龟龟币（存款）
        return self.service.render_rank_of(
            player,
            await self.service.rank_of(player, by_coins=True),
        )

    # ------------------------------------------------------------- 自动签到

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @whitelist_only
    async def auto_check_in(self, event: AstrMessageEvent):
        """当天首条群消息静默签到；出声策略由 checkin.auto_checkin_reply 决定。"""
        if not self.settings.checkin.auto_checkin:
            return
        if self._dispatchable_command(event) is not None:
            # 这条消息会有自己的回复（本插件指令），别再插一张签到卡
            return
        if near_miss_command(event, ALL_COMMAND_NAMES) is not None:
            # 打错指令名的那条也会回一句提示，同样别插卡
            return
        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return
        if not await self._ensure_ready():
            return
        identity = identity_from_event(event)
        try:
            outcome = await self.service.check_in(identity, source="auto")
        except Exception:
            # 自动签到绝不能把正常聊天变成报错。
            logger.exception(f"[{PLUGIN_NAME}] 自动签到失败")
            return
        if outcome.already_checked:
            return
        # 无论出不出声都留一条日志：这是"自动签到到底有没有生效"的唯一凭据。
        logger.info(
            f"[{PLUGIN_NAME}] 自动签到 group={identity.group_id} user={identity.user_id} "
            f"streak={outcome.streak_days} coins=+{outcome.coins_gain}",
        )
        reply = auto_checkin_reply(
            outcome,
            mode=self.settings.checkin.auto_checkin_reply,
            woken=bool(event.is_at_or_wake_command),
        )
        if reply:
            yield event.plain_result(reply)

    # ------------------------------------------------------------- LLM 拦截

    @filter.on_waiting_llm_request(priority=1000)
    async def block_commands_outside_whitelist(self, event: AstrMessageEvent) -> None:
        """白名单外发来的本插件指令不该被默认 LLM 接走。"""
        if event.get_extra("provider_request") is not None:
            return
        if not looks_like_command(event, ALL_COMMAND_NAMES):
            return
        if in_whitelist(self.settings, event):
            return
        event.stop_event()

    # ------------------------------------------------------------- 导入页 API

    def _register_web_apis(self) -> None:
        if not WEB_API_AVAILABLE:
            logger.warning(
                f"[{PLUGIN_NAME}] 当前 AstrBot 未提供 astrbot.api.web，数据导入页不可用",
            )
            return
        register_api = getattr(self.context, "register_web_api", None)
        if register_api is None:  # pragma: no cover - 取决于宿主版本
            logger.warning(f"[{PLUGIN_NAME}] 宿主未提供 register_web_api，数据导入页不可用")
            return
        register_api(
            f"/{PLUGIN_NAME}/import/list",
            self.web_import_list,
            ["GET"],
            "待导入文件与上次导入报告",
        )
        register_api(
            f"/{PLUGIN_NAME}/import/upload",
            self.web_import_upload,
            ["POST"],
            "上传并预览老插件导出文件",
        )
        register_api(
            f"/{PLUGIN_NAME}/import/preview",
            self.web_import_preview,
            ["POST"],
            "预览 import/ 目录里已有的文件",
        )
        register_api(
            f"/{PLUGIN_NAME}/import/apply",
            self.web_import_apply,
            ["POST"],
            "确认导入并补发龟龟币",
        )

    async def web_import_list(self):
        if not await self._ensure_ready():
            return error_response("数据库不可用，请查看 AstrBot 日志", status_code=500)
        return json_response(await webapi.list_pending(self.database, self.data_dir))

    async def web_import_upload(self):
        if not await self._ensure_ready():
            return error_response("数据库不可用，请查看 AstrBot 日志", status_code=500)
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少上传文件", status_code=400)
        raw = await upload.read()
        try:
            path = webapi.stage_upload(
                self.data_dir,
                upload.filename or "",
                raw,
                max_bytes=self.settings.import_.max_file_bytes,
            )
            report = await webapi.preview_import(
                self.database,
                self.settings,
                self.data_dir,
                path.name,
            )
        except webapi.ImportInputError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 预览导入失败")
            return error_response("预览失败，请查看 AstrBot 日志", status_code=500)
        return json_response({"filename": path.name, "preview": report.as_dict()})

    async def web_import_apply(self):
        if not await self._ensure_ready():
            return error_response("数据库不可用，请查看 AstrBot 日志", status_code=500)
        payload = await request.json(default={})
        filename = str((payload or {}).get("filename") or "")
        try:
            report = await webapi.apply_import(
                self.database,
                self.settings,
                self.data_dir,
                filename,
            )
        except webapi.ImportInputError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 导入失败")
            return error_response("导入失败，请查看 AstrBot 日志", status_code=500)
        logger.info(f"[{PLUGIN_NAME}] {webapi.describe(report)}")
        return json_response(report.as_dict())

    async def web_import_preview(self):
        if not await self._ensure_ready():
            return error_response("数据库不可用，请查看 AstrBot 日志", status_code=500)
        payload = await request.json(default={})
        filename = str((payload or {}).get("filename") or "")
        try:
            report = await webapi.preview_import(
                self.database,
                self.settings,
                self.data_dir,
                filename,
            )
        except webapi.ImportInputError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 预览导入失败")
            return error_response("预览失败，请查看 AstrBot 日志", status_code=500)
        return json_response(report.as_dict())


def target_identity_for(
    identity: PlayerIdentity,
    user_id: str,
    nickname: str = "",
) -> PlayerIdentity:
    """被 @ 的群成员身份：沿用当前会话的平台与群号，昵称取群名片。"""
    return PlayerIdentity(
        platform=identity.platform,
        group_id=identity.group_id,
        user_id=user_id,
        nickname=nickname,
    )
