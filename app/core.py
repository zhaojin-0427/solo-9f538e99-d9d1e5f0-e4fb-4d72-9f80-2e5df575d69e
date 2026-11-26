"""补印核心规则：纸张合用判定与全册可配性检查（二分图匹配）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signature:
    """书帖：一帖有正、反两个版号。"""

    signature_no: str
    front_plate: str
    back_plate: str


@dataclass(frozen=True)
class Sheet:
    """纸张：printed_plate 为 None 表示空白纸。"""

    sheet_no: str
    printed_plate: str | None


def prints_for(sig: Signature, sheet: Sheet) -> list[dict[str, str]] | None:
    """判断纸张对该帖是否合用；合用则返回待印面与版号清单，否则返回 None。

    - 空白纸：正、反两面都印；
    - 已印版号对上正面：补印反面；
    - 已印版号对上反面：补印正面；
    - 对不上任何一面：不合用。
    """
    if sheet.printed_plate is None:
        return [
            {"side": "正面", "plate": sig.front_plate},
            {"side": "反面", "plate": sig.back_plate},
        ]
    if sheet.printed_plate == sig.front_plate:
        return [{"side": "反面", "plate": sig.back_plate}]
    if sheet.printed_plate == sig.back_plate:
        return [{"side": "正面", "plate": sig.front_plate}]
    return None


def max_matching(signatures: list[Signature], sheets: list[Sheet]) -> dict[str, str]:
    """书帖—纸张二分图最大匹配（增广路法），返回 {帖号: 纸号}。"""
    adjacency = {
        sig.signature_no: [s.sheet_no for s in sheets if prints_for(sig, s) is not None]
        for sig in signatures
    }
    sheet_owner: dict[str, str] = {}  # 纸号 -> 帖号

    def augment(signature_no: str, seen: set[str]) -> bool:
        for sheet_no in adjacency[signature_no]:
            if sheet_no in seen:
                continue
            seen.add(sheet_no)
            owner = sheet_owner.get(sheet_no)
            if owner is None or augment(owner, seen):
                sheet_owner[sheet_no] = signature_no
                return True
        return False

    for sig in signatures:
        augment(sig.signature_no, set())
    return {sheet_no: sig_no for sheet_no, sig_no in sheet_owner.items()}


def feasible(signatures: list[Signature], sheets: list[Sheet]) -> bool:
    """每帖都能各配一张合用纸张（全册配得齐）时为 True。"""
    return len(max_matching(signatures, sheets)) == len(signatures)
