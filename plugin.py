"""MaiBot QQ 跨群好感度插件。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from maibot_sdk import Command, EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import EventType, HookMode, HookOrder

from .identity import Identity, extract_identity
from .llm_client import LLMClientError, OpenAICompatibleClient
from .prompt_builder import append_block, build_affinity_block
from .scoring import Group, ScoreWeights, build_groups, capped_delta, clamp_score, group_for_score, local_delta
from .storage import AffinityStorage


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "heart"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True, title="启用插件", description="是否启用好感度插件", json_schema_extra={"label": "启用插件"}
    )
    config_version: str = Field(
        default="1.0.0", title="配置版本", description="配置版本", json_schema_extra={"label": "配置版本"}
    )
    initial_score: float = Field(
        default=20.0, title="初始好感度", description="新用户初始好感度", json_schema_extra={"label": "初始好感度"}
    )
    max_context_users: int = Field(
        default=20, title="最多注入用户数", description="规划器最多注入用户数", json_schema_extra={"label": "最多注入用户数"}
    )
    max_prompt_length: int = Field(
        default=2000, title="注入最大长度", description="规划器注入最大长度", json_schema_extra={"label": "注入最大长度"}
    )


class BatchConfig(PluginConfigBase):
    __ui_label__ = "批处理"
    __ui_icon__ = "layers"
    __ui_order__ = 1

    update_interval_seconds: int = Field(
        default=300, title="更新间隔（秒）", description="批量更新间隔（秒）", json_schema_extra={"label": "更新间隔（秒）"}
    )
    max_users_per_request: int = Field(
        default=50, title="单次请求最大用户数", description="单次模型请求最大用户数", json_schema_extra={"label": "单次请求最大用户数"}
    )
    max_messages_per_user: int = Field(
        default=30, title="每用户最大消息数", description="每个用户最多提交的消息数", json_schema_extra={"label": "每用户最大消息数"}
    )
    max_pending_messages: int = Field(
        default=1000, title="待处理消息上限", description="临时消息上限", json_schema_extra={"label": "待处理消息上限"}
    )


class ScoringConfig(PluginConfigBase):
    __ui_label__ = "评分"
    __ui_icon__ = "gauge"
    __ui_order__ = 2

    normal_message_delta: float = Field(
        default=0.02, title="普通发言分值", description="普通发言分值", json_schema_extra={"label": "普通发言分值"}
    )
    mention_bot_delta: float = Field(
        default=0.3, title="@机器人分值", description="@机器人分值", json_schema_extra={"label": "@机器人分值"}
    )
    reply_bot_delta: float = Field(
        default=0.4, title="回复机器人分值", description="回复机器人分值", json_schema_extra={"label": "回复机器人分值"}
    )
    continued_conversation_delta: float = Field(
        default=0.2, title="延续对话分值", description="延续对话分值", json_schema_extra={"label": "延续对话分值"}
    )
    helpful_interaction_delta: float = Field(
        default=0.5, title="帮助性互动分值", description="帮助性互动分值", json_schema_extra={"label": "帮助性互动分值"}
    )
    duplicate_message_delta: float = Field(
        default=-0.2, title="重复消息分值", description="重复消息分值", json_schema_extra={"label": "重复消息分值"}
    )
    spam_delta: float = Field(
        default=-0.3, title="刷屏分值", description="刷屏分值", json_schema_extra={"label": "刷屏分值"}
    )
    max_positive_delta_per_period: float = Field(
        default=5.0, title="单批最大增加", description="单批最大增加", json_schema_extra={"label": "单批最大增加"}
    )
    max_negative_delta_per_period: float = Field(
        default=-5.0, title="单批最大减少", description="单批最大减少", json_schema_extra={"label": "单批最大减少"}
    )
    max_positive_delta_per_day: float = Field(
        default=10.0, title="每日最大增加", description="每日最大增加", json_schema_extra={"label": "每日最大增加"}
    )
    max_negative_delta_per_day: float = Field(
        default=-10.0, title="每日最大减少", description="每日最大减少", json_schema_extra={"label": "每日最大减少"}
    )
    llm_max_delta_per_period: float = Field(
        default=5.0, title="模型单批最大变化", description="模型单批最大变化", json_schema_extra={"label": "模型单批最大变化"}
    )


class DecayConfig(PluginConfigBase):
    __ui_label__ = "长期衰减"
    __ui_icon__ = "trending-down"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False, title="启用长期衰减", description="是否启用长期不互动衰减", json_schema_extra={"label": "启用长期衰减"}
    )
    inactive_days: int = Field(
        default=30, title="不活跃天数", description="开始衰减前的不活跃天数", json_schema_extra={"label": "不活跃天数"}
    )
    daily_decay: float = Field(
        default=0.5, title="每日衰减分值", description="每日衰减分值", json_schema_extra={"label": "每日衰减分值"}
    )
    max_decay_per_period: float = Field(
        default=2.0, title="单批最大衰减", description="单批最大衰减", json_schema_extra={"label": "单批最大衰减"}
    )


class LLMConfig(PluginConfigBase):
    __ui_label__ = "大语言模型"
    __ui_icon__ = "brain"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False, title="启用自定义模型", description="是否启用自定义模型", json_schema_extra={"label": "启用自定义模型"}
    )
    base_url: str = Field(
        default="https://api.openai.com/v1", title="接口地址", description="OpenAI 兼容接口地址", json_schema_extra={"label": "接口地址"}
    )
    api_key: str = Field(
        default="", title="API 密钥", description="API 密钥（敏感信息）", secret=True, json_schema_extra={"label": "API 密钥"}
    )
    model: str = Field(
        default="gpt-4o-mini", title="模型名称", description="模型名称", json_schema_extra={"label": "模型名称"}
    )
    timeout_seconds: int = Field(
        default=30, title="请求超时（秒）", description="请求超时秒数", json_schema_extra={"label": "请求超时（秒）"}
    )
    temperature: float = Field(
        default=0.2, title="温度", description="模型温度", json_schema_extra={"label": "温度"}
    )
    max_tokens: int = Field(
        default=2000, title="最大令牌数", description="最大输出令牌数", json_schema_extra={"label": "最大令牌数"}
    )
    send_message_content: bool = Field(
        default=False, title="发送消息内容", description="是否将截断消息摘要发送给模型", json_schema_extra={"label": "发送消息内容"}
    )


class PermissionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 5

    admin_user_ids: list[str] = Field(
        default_factory=list, title="管理员 QQ 号白名单", description="管理员 QQ 号白名单", json_schema_extra={"label": "管理员 QQ 号白名单"}
    )
    allow_platform_admin: bool = Field(
        default=True, title="允许平台管理员", description="允许平台群管理员操作", json_schema_extra={"label": "允许平台管理员"}
    )
    allow_admin_query_other: bool = Field(
        default=True, title="允许查询他人", description="管理员查询他人", json_schema_extra={"label": "允许查询他人"}
    )
    allow_admin_modify_other: bool = Field(
        default=True, title="允许修改他人", description="管理员修改他人", json_schema_extra={"label": "允许修改他人"}
    )


class GroupConfig(PluginConfigBase):
    __ui_label__ = "好感度分组"
    __ui_icon__ = "users"
    __ui_order__ = 6

    id: str = Field(default="stranger", title="分组 ID", description="分组 ID", json_schema_extra={"label": "分组 ID"})
    name: str = Field(default="陌生人", title="分组名称", description="分组名称", json_schema_extra={"label": "分组名称"})
    min_score: int = Field(default=0, title="最低分", description="最低分", json_schema_extra={"label": "最低分"})
    max_score: int = Field(default=25, title="最高分", description="最高分", json_schema_extra={"label": "最高分"})


class AffinityPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    decay: DecayConfig = Field(default_factory=DecayConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)
    groups: list[GroupConfig] = Field(default_factory=lambda: [
        GroupConfig(id="stranger", name="陌生人", min_score=0, max_score=25),
        GroupConfig(id="acquaintance", name="认识", min_score=26, max_score=50),
        GroupConfig(id="friend", name="朋友", min_score=51, max_score=75),
        GroupConfig(id="trusted", name="信任", min_score=76, max_score=100),
    ], description="可自定义数量的好感度分组", json_schema_extra={"label": "好感度分组"})


class AffinityPlugin(MaiBotPlugin):
    config_model = AffinityPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._groups: tuple[Group, ...] = ()
        self._storage: AffinityStorage | None = None
        self._batch_task: asyncio.Task[None] | None = None
        self._batch_lock = asyncio.Lock()
        self._llm: OpenAICompatibleClient | None = None

    @classmethod
    def build_config_schema(cls, **kwargs: Any) -> dict[str, Any]:
        schema = super().build_config_schema(**kwargs)
        general = schema.get("sections", {}).get("general")
        if isinstance(general, dict):
            general["name"] = "."
            general["title"] = "QQ 好感度"
        return schema

    def _value(self, section: str, field: str, default: Any = None) -> Any:
        value = getattr(self.config, section, None)
        return getattr(value, field, default) if value is not None else default

    def _rebuild(self) -> None:
        raw_groups = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in self.config.groups]
        self._groups = build_groups(raw_groups)
        llm = self.config.llm
        self._llm = OpenAICompatibleClient(
            llm.base_url, llm.api_key, llm.model, llm.timeout_seconds, llm.temperature, llm.max_tokens
        ) if llm.enabled and llm.base_url and llm.model else None

    async def on_load(self) -> None:
        self._rebuild()
        runtime_paths = self.ctx.paths
        self._storage = AffinityStorage(runtime_paths.data_dir, getattr(runtime_paths, "runtime_dir", None))
        await self._storage.load()
        self._batch_task = asyncio.create_task(self._batch_loop())
        self.ctx.logger.info("QQ 好感度插件已加载")

    async def on_unload(self) -> None:
        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
            self._batch_task = None
        self._storage = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        self._rebuild()

    @EventHandler("affinity_message", event_type=EventType.ON_MESSAGE, intercept_message=False, weight=-100)
    async def handle_message(self, message: Any = None, **kwargs: Any) -> None:
        if not self._storage or not self.config.plugin.enabled:
            return
        try:
            identity = extract_identity(message, kwargs=kwargs)
            if identity is None or not self._storage.mark_message_seen(identity.message_id):
                return
            self._storage.add_event(identity.qq_id, self._event_from_payload(identity, message, kwargs))
        except Exception as exc:
            self.ctx.logger.warning(f"好感度消息采集跳过: {type(exc).__name__}")

    def _event_from_payload(self, identity: Identity, message: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        text = str(kwargs.get("text") or getattr(message, "plain_text", "") or getattr(message, "text", "") or "")
        llm_cfg = self.config.llm
        return {
            "message_count": 1,
            "mention_bot_count": int(bool(kwargs.get("mention_bot") or kwargs.get("is_mentioned"))),
            "reply_bot_count": int(bool(kwargs.get("reply_bot") or kwargs.get("is_reply_to_bot"))),
            "continued_count": int(bool(kwargs.get("continued_conversation"))),
            "helpful_count": int(bool(kwargs.get("helpful_interaction"))),
            "duplicate_count": int(bool(kwargs.get("is_duplicate"))),
            "spam_count": int(bool(kwargs.get("is_spam") or kwargs.get("spam"))),
            "group_id": identity.group_id,
            "summary": text if llm_cfg.send_message_content else "",
        }

    async def _batch_loop(self) -> None:
        while True:
            await asyncio.sleep(max(10, self.config.batch.update_interval_seconds))
            await self._run_batch()

    async def _run_batch(self) -> None:
        if not self._storage or self._batch_lock.locked():
            return
        async with self._batch_lock:
            pending = self._storage.exchange_pending()
            if not pending:
                return
            scoring = self.config.scoring
            weights = ScoreWeights(
                normal_message=scoring.normal_message_delta,
                mention_bot=scoring.mention_bot_delta,
                reply_bot=scoring.reply_bot_delta,
                continued_conversation=scoring.continued_conversation_delta,
                helpful_interaction=scoring.helpful_interaction_delta,
                duplicate_message=scoring.duplicate_message_delta,
                spam=scoring.spam_delta,
            )
            llm_deltas: dict[str, float] = {}
            if self._llm:
                users = [{"qq_id": qq_id, "stats": event, "summaries": event.get("summaries", [])} for qq_id, event in list(pending.items())[: self.config.batch.max_users_per_request]]
                try:
                    llm_deltas = await self._llm.evaluate(users, scoring.llm_max_delta_per_period)
                except LLMClientError as exc:
                    self.ctx.logger.warning(f"好感度模型批处理失败，使用本地评分: {exc}")
            now = datetime.now(timezone.utc).isoformat()
            for qq_id, event in pending.items():
                user = self._storage.get_user(qq_id, self.config.plugin.initial_score)
                local = capped_delta(local_delta(event, weights), scoring.max_positive_delta_per_period, scoring.max_negative_delta_per_period)
                semantic = capped_delta(llm_deltas.get(qq_id, 0.0), scoring.llm_max_delta_per_period, -scoring.llm_max_delta_per_period)
                total = capped_delta(local + semantic, scoring.max_positive_delta_per_period, scoring.max_negative_delta_per_period)
                await self._storage.update_user(qq_id, clamp_score(float(user.get("score", self.config.plugin.initial_score)) + total), message_count=int(event.get("message_count", 0)), last_seen_at=now)

    def _is_admin(self, kwargs: dict[str, Any], qq_id: str) -> bool:
        permission = self.config.permission
        if qq_id in {str(item) for item in permission.admin_user_ids}:
            return True
        return bool(permission.allow_platform_admin and (kwargs.get("is_admin") or kwargs.get("is_group_admin") or kwargs.get("is_owner")))

    async def _reply(self, reply_text: str, context: dict[str, Any]) -> None:
        """将命令结果发送到触发命令的对话中。

        context 直接收命令的 kwargs 字典，避免与其中的 ``text`` 等字段撞名。
        """
        stream_id = str(context.get("stream_id") or context.get("chat_id") or "")
        if not stream_id:
            self.ctx.logger.warning("好感度命令缺少 stream_id，无法发送回复")
            return
        try:
            await self.ctx.send.text(reply_text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning(f"好感度命令回复发送失败: {type(exc).__name__}: {exc}")

    @staticmethod
    def _matched(context: dict[str, Any], key: str) -> str:
        """从命令的 matched_groups 中读取正则捕获组。

        运行时把命令参数整体作为 kwargs 展开，正则捕获组统一放在
        ``matched_groups`` 字典里，不会成为顶层关键字参数。
        """
        groups = context.get("matched_groups")
        if isinstance(groups, dict):
            return str(groups.get(key) or "").strip()
        return ""

    def _describe(self, qq_id: str, score: float) -> str:
        group = group_for_score(score, self._groups)
        return f"QQ {qq_id}：好感度 {score:.2f}，关系：{group.name}"

    def _resolve_operator(self, kwargs: dict[str, Any]) -> tuple[str, str]:
        """返回 (操作者 QQ 号, 错误提示)。"""
        if not self._storage:
            return "", "好感度插件尚未加载。"
        identity = extract_identity(kwargs.get("message"), kwargs=kwargs)
        operator = identity.qq_id if identity else ""
        if not operator:
            return "", "无法确认你的 QQ 号。"
        return operator, ""

    async def _query_command(self, target: str, context: dict[str, Any]) -> str:
        kwargs = context
        operator, error = self._resolve_operator(kwargs)
        if error or self._storage is None:
            return error or "好感度插件尚未加载。"
        target = str(target or "").strip()
        if target and target != operator and not self._is_admin(kwargs, operator):
            return "只有管理员可以查询他人好感度。"
        target = target or operator
        user = self._storage.get_user(target, self.config.plugin.initial_score)
        return self._describe(target, float(user["score"]))

    async def _modify_command(self, target: str, value: str, mode: str, context: dict[str, Any]) -> str:
        """管理员修改指定 QQ 的好感度。mode 取 set / add / sub。"""
        kwargs = context
        operator, error = self._resolve_operator(kwargs)
        if error or self._storage is None:
            return error or "好感度插件尚未加载。"
        if not self._is_admin(kwargs, operator):
            return "只有管理员可以修改好感度。"
        target = str(target or "").strip()
        if not target:
            return "请指定要修改的 QQ 号。"
        if target != operator and not self.config.permission.allow_admin_modify_other:
            return "当前配置不允许管理员修改他人好感度。"
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "分数格式不正确，请提供数字。"

        user = self._storage.get_user(target, self.config.plugin.initial_score)
        old_score = float(user["score"])
        if mode == "set":
            new_score = clamp_score(amount)
        elif mode == "add":
            new_score = clamp_score(old_score + amount)
        else:
            new_score = clamp_score(old_score - amount)

        updated = await self._storage.update_user(target, new_score, last_seen_at=datetime.now(timezone.utc).isoformat())
        await self._storage.audit_change(operator, target, mode, old_score, float(updated["score"]))
        return f"QQ {target}：好感度 {old_score:.2f} → {float(updated['score']):.2f}，关系：{group_for_score(float(updated['score']), self._groups).name}"

    @Command(
        "affinity",
        description="查询好感度；管理员可查询指定 QQ",
        pattern=r"^/(?:好感度|affinity)(?:\s+(?P<target>\d+))?\s*$",
    )
    async def command_query(self, **kwargs: Any) -> tuple[bool, str, int]:
        result = await self._query_command(self._matched(kwargs, "target"), kwargs)
        await self._reply(result, kwargs)
        return True, result, 2

    @Command(
        "affinity_set",
        description="管理员将指定 QQ 的好感度设置为固定分数",
        pattern=r"^/(?:好感度设置|affinity_set)\s+(?P<target>\d+)\s+(?P<value>-?\d+(?:\.\d+)?)\s*$",
    )
    async def command_set(self, **kwargs: Any) -> tuple[bool, str, int]:
        result = await self._modify_command(self._matched(kwargs, "target"), self._matched(kwargs, "value"), "set", kwargs)
        await self._reply(result, kwargs)
        return True, result, 2

    @Command(
        "affinity_add",
        description="管理员为指定 QQ 增加好感度",
        pattern=r"^/(?:好感度增加|affinity_add)\s+(?P<target>\d+)\s+(?P<value>-?\d+(?:\.\d+)?)\s*$",
    )
    async def command_add(self, **kwargs: Any) -> tuple[bool, str, int]:
        result = await self._modify_command(self._matched(kwargs, "target"), self._matched(kwargs, "value"), "add", kwargs)
        await self._reply(result, kwargs)
        return True, result, 2

    @Command(
        "affinity_sub",
        description="管理员为指定 QQ 减少好感度",
        pattern=r"^/(?:好感度减少|affinity_sub)\s+(?P<target>\d+)\s+(?P<value>-?\d+(?:\.\d+)?)\s*$",
    )
    async def command_sub(self, **kwargs: Any) -> tuple[bool, str, int]:
        result = await self._modify_command(self._matched(kwargs, "target"), self._matched(kwargs, "value"), "sub", kwargs)
        await self._reply(result, kwargs)
        return True, result, 2


def create_plugin() -> AffinityPlugin:
    return AffinityPlugin()
