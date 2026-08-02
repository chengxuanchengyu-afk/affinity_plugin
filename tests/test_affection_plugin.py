from __future__ import annotations

from pathlib import Path
from typing import Any

import json

import pytest

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.affection_plugin.openai_client import OpenAIResponse, normalize_chat_completions_url
from plugins.affection_plugin.plugin import AffectionPlugin, cents_to_text


class FakeHost:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str]] = []

    async def rpc_call(
        self,
        method: str,
        plugin_id: str,
        payload: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        del plugin_id, timeout_ms
        assert method == "cap.call"
        capability = str((payload or {}).get("capability") or "")
        args = (payload or {}).get("args") or {}
        if capability == "person.get_id":
            return {"success": True, "person_id": f"sid-{args['user_id']}"}
        if capability == "send.text":
            self.sent_messages.append((str(args["stream_id"]), str(args["text"])))
            return {"success": True}
        raise AssertionError(f"未实现的测试能力: {capability}")


class FakeModelClient:
    def __init__(self, delta: float = 1.25) -> None:
        self.delta = delta

    async def generate_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> OpenAIResponse:
        del max_tokens
        user_prompt = messages[-1]["content"]
        input_text = user_prompt.split("输入数据：\n", 1)[1].split("\n输出格式：", 1)[0]
        input_payload = json.loads(input_text)
        updates = [
            {"user_key": user["user_key"], "delta": self.delta, "reason": "测试友好交流"}
            for user in input_payload["users"]
        ]
        return OpenAIResponse(content=json.dumps({"updates": updates}, ensure_ascii=False), model="fake-model")


class FailingModelClient:
    async def generate_json(self, messages: list[dict[str, str]], max_tokens: int = 2000) -> OpenAIResponse:
        del messages, max_tokens
        raise RuntimeError("测试模型故障")


def build_config() -> dict[str, Any]:
    config = AffectionPlugin.build_default_config()
    config["model"].update(
        {
            "base_url": "https://example.com/v1",
            "api_key": "test-key",
            "model_name": "fake-model",
            "settlement_interval_minutes": 60,
            "retry_count": 0,
        }
    )
    config["admins"]["qq_ids"] = ["90001"]
    return config


async def create_test_plugin(tmp_path: Path) -> tuple[AffectionPlugin, FakeHost]:
    host = FakeHost()
    plugin = AffectionPlugin()
    plugin.set_plugin_config(build_config())
    plugin._set_context(
        PluginContext(
            "maibot-community.affinity-plugin",
            rpc_call=host.rpc_call,
            paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"),
        )
    )
    await plugin.on_load()
    return plugin, host


def build_group_message(
    *,
    message_id: str,
    user_id: str = "10001",
    group_id: str = "20001",
    group_card: str | None = "群名片",
    nickname: str = "QQ昵称",
    text: str = "你好",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "timestamp": "1722500000.0",
        "platform": "qq",
        "session_id": f"session-{group_id}",
        "message_info": {
            "user_info": {
                "user_id": user_id,
                "user_nickname": nickname,
                "user_cardname": group_card,
            },
            "group_info": {"group_id": group_id, "group_name": f"群-{group_id}"},
        },
        "raw_message": [{"type": "text", "data": text}],
        "processed_plain_text": text,
    }


@pytest.mark.asyncio
async def test_cross_group_user_reuses_global_affection(tmp_path: Path) -> None:
    plugin, _host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(build_group_message(message_id="m1"))
        person = plugin.store.get_person("qq", "10001")
        assert person is not None
        plugin.store.set_score(
            person=person,
            new_score_cents=3125,
            group_name=plugin._group_for_score(3125),
            operator_platform="qq",
            operator_user_id="90001",
            reason="测试",
        )

        await plugin.record_group_user(
            build_group_message(
                message_id="m2",
                group_id="20002",
                group_card=None,
                nickname="修改后的昵称",
            )
        )
        updated = plugin.store.get_person("qq", "10001")
        assert updated is not None
        assert updated.score_cents == 3125
        assert updated.group_name == "朋友"
        assert updated.qq_nickname == "修改后的昵称"
        assert plugin.store.count_memberships("qq", "10001") == 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_planner_relation_index_uses_message_id_not_nickname(tmp_path: Path) -> None:
    plugin, _host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(
            build_group_message(message_id="exact-message", group_card=None, nickname="重复昵称")
        )
        result = await plugin.inject_planner_affection(
            session_id="session-20001",
            messages=[
                {"role": "system", "content": "稳定系统提示词"},
                {
                    "role": "user",
                    "content": '<message msg_id="exact-message" time="12:00:00" user="重复昵称">\n你好',
                },
                {"role": "assistant", "content": "最终提醒"},
            ],
        )
        modified = result["modified_kwargs"]["messages"]
        relation_message = modified[-2]["content"]
        assert "msg_id=exact-message" in relation_message
        assert "QQ=10001" in relation_message
        assert "SID=sid-10001" in relation_message
        assert "分组=陌生人" in relation_message
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_settlement_applies_decimal_delta_and_clears_cache(tmp_path: Path) -> None:
    plugin, _host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(build_group_message(message_id="m-settle", text="今天很开心，谢谢你"))
        plugin._create_model_client = lambda: FakeModelClient(delta=1.25)  # type: ignore[method-assign]
        summary = await plugin._settle_once(test_when_empty=False)
        assert summary is not None
        assert summary.user_count == 1
        assert summary.event_count == 1
        person = plugin.store.get_person("qq", "10001")
        assert person is not None
        assert cents_to_text(person.score_cents) == "1.25"
        assert plugin.journal.count() == 0
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_failed_settlement_keeps_pending_cache(tmp_path: Path) -> None:
    plugin, _host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(build_group_message(message_id="m-failure", text="需要保留的消息"))
        plugin._create_model_client = lambda: FailingModelClient()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="测试模型故障"):
            await plugin._settle_once(test_when_empty=False)
        person = plugin.store.get_person("qq", "10001")
        assert person is not None
        assert person.score_cents == 0
        assert plugin.journal.count() == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_replyer_hook_uses_exact_message_identity(tmp_path: Path) -> None:
    plugin, _host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(build_group_message(message_id="target-message", nickname="同名用户"))
        result = await plugin.inject_replyer_affection(
            session_id="session-20001",
            reply_message_id="target-message",
            reply_tool_args={"reply_guide": "保持简短"},
        )
        updated_args = result["modified_kwargs"]["reply_tool_args"]
        assert "QQ号=10001" in updated_args["reply_guide"]
        assert "关系分组为“陌生人”" in updated_args["reply_guide"]
        assert "保持简短" in updated_args["reply_guide"]
    finally:
        await plugin.on_unload()


def test_group_validation_rejects_gap() -> None:
    plugin = AffectionPlugin()
    config = build_config()
    config["groups"][1]["min_score"] = 25.02
    plugin.set_plugin_config(config)
    with pytest.raises(ValueError, match="不能重叠或留空"):
        plugin._validate_and_build_groups()


@pytest.mark.asyncio
async def test_command_result_is_sent_to_current_session(tmp_path: Path) -> None:
    plugin, host = await create_test_plugin(tmp_path)
    try:
        await plugin.record_group_user(build_group_message(message_id="m-query"))
        result = await plugin.handle_affection_command(
            stream_id="session-20001",
            platform="qq",
            user_id="10001",
            text="/好感度",
        )
        assert result[0] is True
        assert host.sent_messages
        stream_id, text = host.sent_messages[-1]
        assert stream_id == "session-20001"
        assert "好感度：0.00" in text
        assert "关系分组：陌生人" in text
    finally:
        await plugin.on_unload()


def test_openai_url_normalization() -> None:
    assert normalize_chat_completions_url("https://example.com/v1") == "https://example.com/v1/chat/completions"
    assert (
        normalize_chat_completions_url("https://example.com/v1/chat/completions")
        == "https://example.com/v1/chat/completions"
    )


def test_webui_schema_is_chinese_and_supports_hundredths() -> None:
    schema = AffectionPlugin.build_config_schema(plugin_id="maibot-community.affinity-plugin")
    assert schema["sections"]["model"]["fields"]["api_key"]["ui_type"] == "password"
    assert schema["sections"]["model"]["fields"]["base_url"]["label"] == "接口地址"
    group_fields = schema["sections"]["general"]["fields"]["groups"]["item_fields"]
    assert group_fields["name"]["label"] == "分组名称"
    assert group_fields["min_score"]["step"] == 0.01
    assert group_fields["max_score"]["step"] == 0.01
