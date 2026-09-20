# -*- coding: utf-8 -*-
# deepseek_codex_Repair / repair — 纯修复核心
#
# repair_input_items() 对 OpenAI Responses API 的 input[] 数组做协议修复:
#   Pass 0  清理非法 item（非 dict）
#   Pass 1  补齐 function_call 缺失的 call_id
#   Pass 2  修复 function_call_output 缺失的 call_id（位置配对/合成注入）
#   Pass 3  处理孤立输出（call_id 无匹配 fc → 转消息或移除）
#   Pass 4  终检（悬空 fc 保留、重复 id 仅 debug 日志）
#
# 约束: 本模块只 import 标准库，禁止 import config/proxy/FastAPI/httpx，
#       保证纯函数核心可独立单测。绝不修改调用方传入的列表。

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("app.repair")

OrphanStrategy = Literal["convert_to_user", "convert_to_developer", "remove"]
MissingIdStrategy = Literal["synthesize", "convert_to_user"]


@dataclass(frozen=True)
class RepairConfig:
    """修复策略配置（由 main 从环境变量构建后传入）。"""

    orphan_strategy: OrphanStrategy = "convert_to_user"
    missing_id_strategy: MissingIdStrategy = "synthesize"
    synthetic_call_prefix: str = "call_proxy_"


@dataclass
class RepairEntry:
    """单条修复记录: 规则名 + 原始索引 + 人类可读详情。"""

    rule: str
    index: int
    detail: str


@dataclass
class RepairReport:
    """修复报告: 所有修复条目，count 供响应头 X-Proxy-Repairs 使用。"""

    entries: list[RepairEntry] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.entries)

    def add(self, rule: str, index: int, detail: str) -> None:
        self.entries.append(RepairEntry(rule=rule, index=index, detail=detail))


# ---- 类型判断与取值辅助 ----


def _is_function_call(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call"


def _is_function_call_output(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call_output"


def _get_call_id(item: dict) -> str | None:
    """取合法 call_id；缺失/空串/非字符串一律返回 None。"""
    cid = item.get("call_id")
    if isinstance(cid, str) and cid.strip():
        return cid
    return None


def _unique_id(base: str, used: set[str]) -> str:
    """生成未被占用的 id: base, base_1, base_2 ..."""
    candidate = base
    n = 1
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def _make_synthetic_function_call(config: RepairConfig, call_id: str) -> dict:
    """构造占位 function_call（arguments 为空对象）。"""
    return {
        "type": "function_call",
        "name": "proxy_synthetic",
        "arguments": "{}",
        "call_id": call_id,
    }


def _is_text_parts_list(output: Any) -> bool:
    """output 是否为可直接复用为 message content 的 output_text 部件数组。"""
    return (
        isinstance(output, list)
        and bool(output)
        and all(
            isinstance(part, dict)
            and part.get("type") == "output_text"
            and part.get("text")
            for part in output
        )
    )


def _output_to_text(output: Any) -> str | None:
    """把 output 转为文本；不可修复（None/空）返回 None。"""
    if output is None:
        return None
    if isinstance(output, str):
        return output if output.strip() else None
    if isinstance(output, list):
        if not output:
            return None
        return json.dumps(output, ensure_ascii=False)
    # dict / 数字 / bool 等其他 JSON 值
    return json.dumps(output, ensure_ascii=False)


def _convert_output_to_message(item: dict, role: str) -> dict | None:
    """把 function_call_output 转为普通 message item；不可修复返回 None。"""
    output = item.get("output")
    if _is_text_parts_list(output):
        # output_text 部件数组直接复用为 content，不做字符串化
        return {"type": "message", "role": role, "content": copy.deepcopy(output)}
    text = _output_to_text(output)
    if text is None:
        return None
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text", "text": text}],
    }


class _IdGenerator:
    """合成 call_id 生成器：前缀 + 递增计数，保证不与已有 id 冲突。"""

    def __init__(self, prefix: str, used: set[str]):
        self.prefix = prefix
        self.used = used
        self.counter = 0

    def next(self) -> str:
        base = f"{self.prefix}{self.counter}"
        self.counter += 1
        cid = _unique_id(base, self.used)
        self.used.add(cid)
        return cid


# ---- 各修复趟次 ----
# wrapped 元素为 (orig_index, item_dict)；orig_index 是 item 在原始 input
# 中的位置，删除/插入后日志索引仍可对应原始请求。


def _pass_sanitize_non_dict(source: list, report: RepairReport) -> list[tuple[int, dict]]:
    """Pass 0: 移除非 dict item（不可配置）。"""
    wrapped: list[tuple[int, dict]] = []
    for i, item in enumerate(source):
        if not isinstance(item, dict):
            report.add(
                "remove_non_dict_item", i,
                f"non-dict {type(item).__name__} removed",
            )
            continue
        wrapped.append((i, item))
    return wrapped


def _pass_fix_function_call_ids(
    wrapped: list[tuple[int, dict]],
    config: RepairConfig,
    report: RepairReport,
    generator: _IdGenerator,
) -> None:
    """Pass 1: 为缺少 call_id 的 function_call 生成一个（已有 id 一律不改）。"""
    for orig_i, item in wrapped:
        if not _is_function_call(item):
            continue
        if _get_call_id(item) is None:
            new_id = generator.next()
            item["call_id"] = new_id
            report.add(
                "synthesize_function_call_id", orig_i,
                f"generated {new_id}",
            )


def _pass_adopt_or_inject_output_ids(
    wrapped: list[tuple[int, dict]],
    config: RepairConfig,
    report: RepairReport,
    generator: _IdGenerator,
    fc_ids: set[str],
) -> None:
    """Pass 2: 修复 function_call_output 缺失的 call_id。

    策略（增强式位置配对）:
    1. 输出有合法 call_id 且匹配 fc → 正常配对，不改
    2. 输出有 call_id 但无匹配 fc → 留给 Pass 3 处理孤立
    3. 输出缺 call_id:
       a. 最近的前导 fc 未消费 → 采用其 call_id
       b. 无可用的前导 fc 且 synthesize 策略 → 注入合成 fc + 新 id
       c. convert_to_user 策略 → 转 user 消息
    """
    consumed_ids: set[str] = set()
    last_fc: tuple[int, int, str, bool] | None = None  # (pos, orig_i, call_id, consumed)

    pos = 0
    while pos < len(wrapped):
        orig_i, item = wrapped[pos]

        if _is_function_call(item):
            cid = item.get("call_id")
            last_fc = (pos, orig_i, cid, cid in consumed_ids)
            pos += 1
            continue

        if not _is_function_call_output(item):
            pos += 1
            continue

        cid = _get_call_id(item)
        if cid is not None and cid in fc_ids:
            # 正常配对：标记消费，防止该 fc 再被后续无 id 输出采用
            consumed_ids.add(cid)
            if last_fc is not None and last_fc[2] == cid:
                last_fc = (last_fc[0], last_fc[1], last_fc[2], True)
            pos += 1
            continue

        if cid is not None:
            # 有 id 但无匹配 fc → 孤立，Pass 3 决定转换或移除
            pos += 1
            continue

        # ---- 缺 call_id ----
        if config.missing_id_strategy == "synthesize":
            if last_fc is not None and not last_fc[3]:
                # 采用最近前导 fc 的 call_id（位置配对）
                adopted = last_fc[2]
                item["call_id"] = adopted
                consumed_ids.add(adopted)
                report.add(
                    "adopt_call_id_from_preceding_function_call", orig_i,
                    f"adopted {adopted}",
                )
                last_fc = (last_fc[0], last_fc[1], last_fc[2], True)
                pos += 1
            else:
                # 注入合成 function_call 紧贴输出之前；合成 fc 自身标为已消费，
                # 因此连续两个无 id 输出不会共享同一个合成 pair
                new_id = generator.next()
                synthetic = _make_synthetic_function_call(config, new_id)
                wrapped.insert(pos, (orig_i, synthetic))
                item["call_id"] = new_id
                consumed_ids.add(new_id)
                fc_ids.add(new_id)
                report.add(
                    "inject_synthetic_function_call", orig_i,
                    f"inserted pair {new_id}",
                )
                last_fc = (pos, orig_i, new_id, True)
                pos += 2  # 跳过合成 fc + 本输出
        else:  # convert_to_user
            converted = _convert_output_to_message(item, "user")
            if converted is None:
                report.add(
                    "remove_unfixable_output", orig_i,
                    "no/empty output content",
                )
                wrapped.pop(pos)
                continue
            wrapped[pos] = (orig_i, converted)
            report.add(
                "convert_missing_id_output_to_user", orig_i,
                "missing call_id → user message",
            )
            pos += 1


def _pass_handle_orphans(
    wrapped: list[tuple[int, dict]],
    config: RepairConfig,
    report: RepairReport,
    fc_ids: set[str],
) -> None:
    """Pass 3: 处理孤立输出（call_id 无匹配 fc）。

    按 ORPHAN_STRATEGY: convert_to_user / convert_to_developer / remove。
    不可修复（无 output 内容）时无论策略一律移除。
    """
    pos = 0
    while pos < len(wrapped):
        orig_i, item = wrapped[pos]
        if not _is_function_call_output(item):
            pos += 1
            continue

        cid = _get_call_id(item)
        # Pass 2 已修复的输出（adopt/合成）此时 cid 必在 fc_ids 中
        if cid is None or cid in fc_ids:
            pos += 1
            continue

        if config.orphan_strategy in ("convert_to_user", "convert_to_developer"):
            role = "user" if config.orphan_strategy == "convert_to_user" else "developer"
            converted = _convert_output_to_message(item, role)
            if converted is None:
                report.add(
                    "remove_unfixable_output", orig_i,
                    f"call_id {cid} unmatched, no/empty output content",
                )
                wrapped.pop(pos)
                continue
            wrapped[pos] = (orig_i, converted)
            report.add(
                f"convert_orphan_to_{role}", orig_i,
                f"call_id {cid} unmatched -> {role} message",
            )
        else:  # remove
            report.add(
                "remove_orphan_output", orig_i,
                f"call_id {cid} unmatched",
            )
            wrapped.pop(pos)
            continue

        pos += 1


def _pass_validate(wrapped: list[tuple[int, dict]]) -> None:
    """Pass 4: 终检（默认不改动）。

    - 末尾悬空 function_call → 保留不动（保守：删除可能破坏被截断的多轮对话）
    - 重复的 output call_id → 保留，仅 debug 日志
    """
    if not wrapped:
        return
    last_item = wrapped[-1][1]
    if _is_function_call(last_item):
        logger.debug(
            "dangling function_call at end of input left untouched (call_id=%r)",
            last_item.get("call_id"),
        )
    seen: set[str] = set()
    for _, item in wrapped:
        if _is_function_call_output(item):
            cid = _get_call_id(item)
            if cid is not None:
                if cid in seen:
                    logger.debug("duplicate output call_id %r left as-is", cid)
                seen.add(cid)


# ---- 公开 API ----


def repair_input_items(
    items: Any,
    config: RepairConfig | None = None,
) -> tuple[list, RepairReport]:
    """纯修复 OpenAI Responses API 的 input 数组。

    坏输入不抛异常；绝不修改调用方传入的对象（内部 deepcopy）。

    Args:
        items: Responses API 请求的 input 数组（可能损坏）。
        config: 修复策略配置，None 用默认。

    Returns:
        (修复后的 items 列表, 修复报告)。
    """
    cfg = config or RepairConfig()
    report = RepairReport()

    if not isinstance(items, list):
        # 非列表输入不属于修复范畴，原样返回
        return copy.deepcopy(items), report

    source = copy.deepcopy(items)

    # Pass 0: 清理非法 item
    wrapped = _pass_sanitize_non_dict(source, report)

    # 已占用的 call_id 集合（原 fc 的 + 合成的），供唯一性检查
    used_ids = {
        _get_call_id(item)
        for _, item in wrapped
        if _is_function_call(item) and _get_call_id(item) is not None
    }
    generator = _IdGenerator(cfg.synthetic_call_prefix, used_ids)

    # Pass 1: 补齐 fc 的 call_id
    _pass_fix_function_call_ids(wrapped, cfg, report, generator)

    # fc id 集合（Pass 1 之后完整），供输出配对判定
    fc_ids = {
        item.get("call_id")
        for _, item in wrapped
        if _is_function_call(item) and isinstance(item.get("call_id"), str)
    }

    # Pass 2: 修复输出的 call_id
    _pass_adopt_or_inject_output_ids(wrapped, cfg, report, generator, fc_ids)

    # Pass 3: 处理孤立输出
    _pass_handle_orphans(wrapped, cfg, report, fc_ids)

    # Pass 4: 终检
    _pass_validate(wrapped)

    return [item for _, item in wrapped], report
