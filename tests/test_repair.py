# -*- coding: utf-8 -*-
"""repair_input_items 纯函数单测：21 个表驱动/场景用例，无 HTTP 依赖。"""
from __future__ import annotations

import pytest

from app.repair import RepairConfig, repair_input_items


# ---- 构造辅助 ----


def fc(call_id=None, name="get_weather", arguments="{}"):
    """构造 function_call item；call_id=None 表示缺字段。"""
    item = {"type": "function_call", "name": name, "arguments": arguments}
    if call_id is not None:
        item["call_id"] = call_id
    return item


def fco(call_id=None, output="ok"):
    """构造 function_call_output item；call_id=None 表示缺字段。"""
    item = {"type": "function_call_output", "output": output}
    if call_id is not None:
        item["call_id"] = call_id
    return item


def msg(role="user", content="hi"):
    return {"type": "message", "role": role, "content": content}


def rules(report):
    return [e.rule for e in report.entries]


# ---- 规则 1: 孤立输出 ----


def test_orphan_output_default_convert_to_user():
    items = [fc("call_1"), fco("call_9", "r")]
    repaired, report = repair_input_items(items)
    assert repaired[0] == fc("call_1")
    assert repaired[1] == msg("user", [{"type": "output_text", "text": "r"}])
    assert rules(report) == ["convert_orphan_to_user"]
    assert report.entries[0].index == 1


def test_orphan_output_convert_to_developer():
    cfg = RepairConfig(orphan_strategy="convert_to_developer")
    repaired, report = repair_input_items([fc("call_1"), fco("call_9", "r")], cfg)
    assert repaired[1] == msg("developer", [{"type": "output_text", "text": "r"}])
    assert rules(report) == ["convert_orphan_to_developer"]


def test_orphan_output_remove():
    cfg = RepairConfig(orphan_strategy="remove")
    repaired, report = repair_input_items([fc("call_1"), fco("call_9", "r")], cfg)
    assert repaired == [fc("call_1")]
    assert rules(report) == ["remove_orphan_output"]


def test_unfixable_orphan_removed_even_with_convert_strategy():
    # output=None → 无法转消息，无论策略一律移除
    repaired, report = repair_input_items([fc("call_1"), fco("call_9", output=None)])
    assert repaired == [fc("call_1")]
    assert rules(report) == ["remove_unfixable_output"]


# ---- 规则 2: 缺 call_id 的输出 ----


def test_missing_id_adopts_from_preceding_function_call():
    items = [fc("call_1"), fco(None, "r")]
    repaired, report = repair_input_items(items)
    assert repaired[0] == fc("call_1")
    assert repaired[1]["call_id"] == "call_1"
    assert repaired[1]["output"] == "r"
    assert len(repaired) == 2
    assert rules(report) == ["adopt_call_id_from_preceding_function_call"]
    assert report.entries[0].index == 1


def test_missing_id_at_index0_injects_synthetic_pair():
    repaired, report = repair_input_items([fco(None, "r")])
    assert len(repaired) == 2
    assert repaired[0]["type"] == "function_call"
    assert repaired[0]["call_id"].startswith("call_proxy_")
    assert repaired[1]["type"] == "function_call_output"
    assert repaired[1]["call_id"] == repaired[0]["call_id"]
    assert rules(report) == ["inject_synthetic_function_call"]
    assert report.entries[0].index == 0


def test_missing_id_convert_to_user_strategy():
    cfg = RepairConfig(missing_id_strategy="convert_to_user")
    repaired, report = repair_input_items([fco(None, "r")], cfg)
    assert repaired == [msg("user", [{"type": "output_text", "text": "r"}])]
    assert rules(report) == ["convert_missing_id_output_to_user"]


def test_consecutive_missing_id_outputs_do_not_share_pair():
    # 第一个采用前导 fc 的 id，第二个拿独立合成 pair
    items = [fc("call_1"), fco(None, "a"), fco(None, "b")]
    repaired, report = repair_input_items(items)
    assert len(repaired) == 4
    assert repaired[1]["call_id"] == "call_1"
    assert repaired[2]["type"] == "function_call"  # 注入的合成 fc
    assert repaired[3]["call_id"] == repaired[2]["call_id"]
    assert repaired[2]["call_id"] != "call_1"
    assert rules(report) == [
        "adopt_call_id_from_preceding_function_call",
        "inject_synthetic_function_call",
    ]


def test_consumed_function_call_not_adopted_twice():
    items = [fc("call_1"), fco("call_1", "a"), fco(None, "b")]
    repaired, report = repair_input_items(items)
    assert len(repaired) == 4
    assert repaired[2]["type"] == "function_call"  # 合成，而非复用 call_1
    assert repaired[2]["call_id"] != "call_1"
    assert repaired[3]["call_id"] == repaired[2]["call_id"]
    assert rules(report) == ["inject_synthetic_function_call"]


def test_function_call_missing_id_filled_then_adopted():
    items = [fc(None), fco(None, "r")]
    repaired, report = repair_input_items(items)
    assert repaired[0]["call_id"].startswith("call_proxy_")
    assert repaired[1]["call_id"] == repaired[0]["call_id"]
    assert rules(report) == [
        "synthesize_function_call_id",
        "adopt_call_id_from_preceding_function_call",
    ]


def test_duplicate_existing_ids_untouched():
    items = [fc("call_1"), fc("call_1"), fco(None, "r")]
    repaired, report = repair_input_items(items)
    assert repaired[0]["call_id"] == "call_1"
    assert repaired[1]["call_id"] == "call_1"
    assert repaired[2]["call_id"] == "call_1"  # 采用最近前导 fc
    assert rules(report) == ["adopt_call_id_from_preceding_function_call"]


def test_generated_id_avoids_collision():
    # 已有 fc 占用 call_proxy_0 → 合成的必须是 call_proxy_0_1
    items = [fco(None, "a"), fc("call_proxy_0")]
    repaired, report = repair_input_items(items)
    assert repaired[0]["type"] == "function_call"
    assert repaired[0]["call_id"] == "call_proxy_0_1"
    assert repaired[1]["call_id"] == "call_proxy_0_1"
    assert repaired[2] == fc("call_proxy_0")  # 原有 fc 不动
    assert rules(report) == ["inject_synthetic_function_call"]


# ---- 规则 3: 清理与保护 ----


def test_non_dict_items_removed_with_original_indices():
    items = ["garbage", fc("call_1"), 42]
    repaired, report = repair_input_items(items)
    assert repaired == [fc("call_1")]
    assert rules(report) == ["remove_non_dict_item", "remove_non_dict_item"]
    assert [e.index for e in report.entries] == [0, 2]


def test_unknown_item_types_preserved():
    items = [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "x"}]},
        msg(),
        {"type": "custom", "x": 1},
    ]
    repaired, report = repair_input_items(items)
    assert repaired == items
    assert report.count == 0


def test_well_formed_input_untouched():
    items = [msg("user", "q"), fc("call_1"), fco("call_1", "r")]
    repaired, report = repair_input_items(items)
    assert repaired == items
    assert report.count == 0


def test_dangling_function_call_at_end_kept():
    items = [msg("user", "q"), fc("call_1")]
    repaired, report = repair_input_items(items)
    assert repaired == items
    assert report.count == 0


def test_non_string_call_id_treated_as_missing():
    items = [fc("call_1"), fco(123, "r")]
    repaired, report = repair_input_items(items)
    assert repaired[1]["call_id"] == "call_1"
    assert rules(report) == ["adopt_call_id_from_preceding_function_call"]


def test_output_text_parts_reused_directly_as_content():
    parts = [{"type": "output_text", "text": "hi"}]
    repaired, report = repair_input_items([fco("call_9", output=parts)])
    assert repaired == [msg("user", parts)]
    assert rules(report) == ["convert_orphan_to_user"]


def test_output_json_value_stringified():
    repaired, report = repair_input_items([fco("call_9", output={"k": 1})])
    assert repaired == [msg("user", [{"type": "output_text", "text": '{"k": 1}'}])]
    assert rules(report) == ["convert_orphan_to_user"]


def test_deepcopy_protection():
    original = [fc("call_1"), fco("call_9", "r")]
    repaired, report = repair_input_items(original)
    assert original == [fc("call_1"), fco("call_9", "r")]  # 原列表不变
    assert report.count == 1
    assert repaired[0] is not original[0]  # 无共享对象
    assert repaired[1] is not original[1]


def test_empty_and_garbage_input_never_raises():
    assert repair_input_items([]) == ([], repair_input_items([])[1])
    repaired, report = repair_input_items([])
    assert repaired == [] and report.count == 0
    # 非列表输入不属于修复范畴：原样返回、零修复、不抛异常
    repaired, report = repair_input_items("not a list")
    assert repaired == "not a list" and report.count == 0
    repaired, report = repair_input_items(None)
    assert repaired is None and report.count == 0
    repaired, report = repair_input_items({"type": "message"})
    assert repaired == {"type": "message"} and report.count == 0


# ---- 规则: reasoning_text 回注（DeepSeek 思考模式） ----


def _lookup_mapping(mapping: dict) -> callable:
    """构造按 call_id / 消息内容查找的 lookup。"""

    def lookup(item):
        cid = item.get("call_id")
        if cid in mapping:
            return mapping[cid]
        if item.get("type") == "message" and isinstance(item.get("content"), str):
            return mapping.get(item["content"])
        return None

    return lookup


def test_restore_reasoning_injected_before_function_call():
    items = [fc("call_1"), fco("call_1", "r")]
    lookup = _lookup_mapping({"call_1": "先查天气"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert len(repaired) == 3
    assert repaired[0] == {
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": "先查天气"}],
    }
    assert repaired[1] == fc("call_1")
    assert repaired[2] == fco("call_1", "r")
    assert rules(report) == ["restore_reasoning_text"]
    assert report.entries[0].index == 0


def test_restore_reasoning_skips_when_already_present():
    items = [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "已有"}]},
        fc("call_1"),
        fco("call_1", "r"),
    ]
    lookup = _lookup_mapping({"call_1": "先查天气"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert repaired == items  # 不重复注入
    assert report.count == 0


def test_restore_reasoning_fixes_non_plaintext_reasoning_item():
    items = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "摘要"}],
         "encrypted_content": "enc"},
        fc("call_1"),
        fco("call_1", "r"),
    ]
    lookup = _lookup_mapping({"call_1": "缓存纯文本"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert repaired[0] == {
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": "缓存纯文本"}],
    }
    assert rules(report) == ["fix_reasoning_item_plaintext"]


def test_restore_reasoning_no_cache_hit_no_injection():
    items = [fc("call_1"), fco("call_1", "r")]
    repaired, report = repair_input_items(items, reasoning_lookup=_lookup_mapping({}))
    assert repaired == items
    assert report.count == 0


def test_restore_reasoning_none_lookup_skips_pass():
    items = [fc("call_1"), fco("call_1", "r")]
    repaired, report = repair_input_items(items)  # 不传 lookup
    assert repaired == items
    assert report.count == 0


def test_restore_reasoning_before_assistant_message():
    items = [msg("assistant", "答案是 42")]
    lookup = _lookup_mapping({"答案是 42": "算一下"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert repaired[0] == {
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": "算一下"}],
    }
    assert repaired[1] == msg("assistant", "答案是 42")
    assert rules(report) == ["restore_reasoning_text"]


def test_restore_reasoning_not_applied_to_user_messages():
    items = [msg("user", "你好")]
    lookup = _lookup_mapping({"你好": "不该注入"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert repaired == items
    assert report.count == 0


def test_restore_reasoning_combined_with_pairing_repairs():
    # 回注 reasoning 的同时，缺 call_id 的配对修复照常工作
    items = [fc("call_1"), fco(None, "r")]
    lookup = _lookup_mapping({"call_1": "先查天气"})
    repaired, report = repair_input_items(items, reasoning_lookup=lookup)
    assert len(repaired) == 3
    assert repaired[0]["type"] == "reasoning"
    assert repaired[2]["call_id"] == "call_1"  # 配对修复仍生效
    assert rules(report) == [
        "restore_reasoning_text",
        "adopt_call_id_from_preceding_function_call",
    ]
