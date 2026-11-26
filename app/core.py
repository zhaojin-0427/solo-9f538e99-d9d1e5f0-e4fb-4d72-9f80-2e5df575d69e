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


# ---------------------------------------------------------------------------
# 急停后的停机进度核对
#
# 每张本轮上机的纸，在急停时刻“已经补印成功了哪几面”是一个待求状态：
# - 原补印指令规定了该纸应补的面（required_sides，含版号）；
# - 原为空白的纸必须“先补正面、再补反面”，所以进度只能是前缀：
#   未补 / 只补正面 / 正面反面都补；
# - 单面纸只有未补 / 已补该面两种；
# - 印机正、反面成功张数是所有上机纸状态的逐面计数之和；
# - 报验给出该纸停机时已补面（按纸号更正即覆盖）；
# - 已发续印单的纸，其发单时确定的进度被冻结，后续核对不得改动。
#
# 核对就是在上述全部约束下求“全局可行解”，再看每张纸在所有可行解里
# 有几种不同进度：多种 -> 待查；唯一 -> 可判定，再按有无缺面分类。
# ---------------------------------------------------------------------------

SIDE_FRONT = "正面"
SIDE_BACK = "反面"

# 各种常见的面名写法 -> 规范写法
SIDE_ALIASES = {
    "正面": SIDE_FRONT,
    "正": SIDE_FRONT,
    "正⾯": SIDE_FRONT,
    "front": SIDE_FRONT,
    "f": SIDE_FRONT,
    "recto": SIDE_FRONT,
    "frontside": SIDE_FRONT,
    "front_side": SIDE_FRONT,
    "faces": SIDE_FRONT,
    "反面": SIDE_BACK,
    "反": SIDE_BACK,
    "背面": SIDE_BACK,
    "back": SIDE_BACK,
    "b": SIDE_BACK,
    "verso": SIDE_BACK,
    "backside": SIDE_BACK,
    "back_side": SIDE_BACK,
}


def normalize_side(value) -> str:
    """把各种面名写法归一为「正面」「反面」；无法识别抛 ValueError。"""
    if value is None:
        raise ValueError("面名不能为空")
    token = str(value).strip().lower().replace(" ", "").replace("_", "")
    # 下划线已被去掉，补一个直接查表的变体
    compact = token.replace("-", "")
    table = {k.replace("_", "").replace("-", ""): v for k, v in SIDE_ALIASES.items()}
    if compact in table:
        return table[compact]
    if str(value).strip() in SIDE_ALIASES:
        return SIDE_ALIASES[str(value).strip()]
    raise ValueError(f"无法识别的印面：{value!r}（应为 正面/反面）")


def normalize_sides(values) -> list[str]:
    """面清单归一、去重并按 正面 -> 反面 的固定顺序返回。"""
    if values is None:
        return []
    if isinstance(values, str):
        # 允许 "正面,反面" / "正面、反面" / "正面 反面"
        import re

        values = [p for p in re.split(r"[,，、\s/]+", values) if p]
    out: list[str] = []
    for v in values:
        side = normalize_side(v)
        if side not in out:
            out.append(side)
    return [s for s in (SIDE_FRONT, SIDE_BACK) if s in out]


@dataclass(frozen=True)
class ProgressFact:
    """一张上机纸参与核对的全部已知约束。"""

    sheet_no: str
    required_sides: tuple[str, ...]
    blank: bool
    reported: frozenset[str] | None = None  # 报验（更正后）的已补面；None=未报验
    frozen: frozenset[str] | None = None  # 已发续印单冻结的停机进度


class ReconcileError(Exception):
    """全部约束下不存在任何可行进度组合（核对无解）。"""

    def __init__(self, reason: str, sheet_no: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.sheet_no = sheet_no


def progress_states_for(fact: ProgressFact) -> list[tuple[str, ...]]:
    """该纸在单纸约束（原指令 + 空白纸正先反后）下可能的进度状态。

    返回有序的面元组列表，如 [(), ("正面",), ("正面", "反面")]。
    报验 / 冻结与这些状态不符时，候选会被清空，由 reconcile 判为无解。
    """
    required = list(fact.required_sides)
    if fact.blank:
        # 空白纸：先正后反，只允许指令顺序的前缀。
        ordered: list[str] = []
        if SIDE_FRONT in required:
            ordered.append(SIDE_FRONT)
        if SIDE_BACK in required:
            ordered.append(SIDE_BACK)
        for s in required:  # 理论上只有正反两面，兜底保留其它面
            if s not in ordered:
                ordered.append(s)
        states: list[tuple[str, ...]] = [()]
        prefix: list[str] = []
        for side in ordered:
            prefix = prefix + [side]
            states.append(tuple(prefix))
    else:
        # 单面补印纸：要么没补，要么已补该面。
        states = [()]
        for side in required:
            states.append((side,))

    def accepted(state: tuple[str, ...]) -> bool:
        as_set = frozenset(state)
        if fact.reported is not None and as_set != fact.reported:
            return False
        if fact.frozen is not None and as_set != fact.frozen:
            return False
        return True

    return [s for s in states if accepted(s)]


def _bits(state: tuple[str, ...]) -> tuple[int, int]:
    return (1 if SIDE_FRONT in state else 0, 1 if SIDE_BACK in state else 0)


def reconcile_progress(
    facts: list[ProgressFact], front_total: int, back_total: int
) -> dict[str, list[tuple[str, ...]]]:
    """在全部约束下核对。

    返回 {纸号: 该纸在所有全局可行解中可能出现的进度状态}（只含真正
    参与某个可行解的状态）。全局无解时抛 ReconcileError。

    用“正、反面累计张数”两维计数做前后向动态规划：纸数 <=48、
    张数 <=48，状态空间很小。
    """
    if front_total < 0 or back_total < 0:
        raise ReconcileError("正、反面成功张数不能为负")

    candidates: list[list[tuple[str, ...]]] = []
    for fact in facts:
        states = progress_states_for(fact)
        if not states:
            if fact.frozen is not None and fact.reported is not None:
                raise ReconcileError(
                    f"纸 {fact.sheet_no} 更正后的报验面 {sorted(fact.reported, key=_side_order)} "
                    f"与已发续印单冻结的停机进度 {sorted(fact.frozen, key=_side_order)} 不一致，核对无解",
                    sheet_no=fact.sheet_no,
                )
            if fact.reported is not None:
                raise ReconcileError(
                    f"纸 {fact.sheet_no} 报验的已补面与原补印指令/补印顺序不符，核对无解",
                    sheet_no=fact.sheet_no,
                )
            raise ReconcileError(
                f"纸 {fact.sheet_no} 的停机进度无合法候选，核对无解",
                sheet_no=fact.sheet_no,
            )
        candidates.append(states)

    zero: set[tuple[int, int]] = {(0, 0)}

    # prefix[i]：前 i 张纸（下标 0..i-1）能凑出的 (正面数, 反面数) 集合
    prefix: list[set[tuple[int, int]]] = [zero]
    for states in candidates:
        reachable: set[tuple[int, int]] = set()
        for f_cnt, b_cnt in prefix[-1]:
            for state in states:
                df, db = _bits(state)
                reachable.add((f_cnt + df, b_cnt + db))
        prefix.append(reachable)

    if (front_total, back_total) not in prefix[len(facts)]:
        raise ReconcileError(
            f"印机记录正面成功 {front_total} 张、反面成功 {back_total} 张，"
            "与原补印指令、报验和已发续印单进度核对不上：不存在任何可行的停机进度组合，核对无解"
        )

    # suffix[i]：第 i 张到最后一张能凑出的计数集合
    suffix: list[set[tuple[int, int]] | None] = [None] * (len(facts) + 1)
    suffix[len(facts)] = zero
    for i in range(len(facts) - 1, -1, -1):
        reachable = set()
        later = suffix[i + 1]
        assert later is not None
        for f_cnt, b_cnt in later:
            for state in candidates[i]:
                df, db = _bits(state)
                reachable.add((f_cnt + df, b_cnt + db))
        suffix[i] = reachable

    result: dict[str, list[tuple[str, ...]]] = {}
    target_f, target_b = front_total, back_total
    for i, fact in enumerate(facts):
        participating: list[tuple[str, ...]] = []
        before = prefix[i]
        after = suffix[i + 1]
        assert after is not None
        for state in candidates[i]:
            df, db = _bits(state)
            for pf, pb in before:
                need = (target_f - pf - df, target_b - pb - db)
                if need in after:
                    participating.append(state)
                    break
        result[fact.sheet_no] = participating
    return result


def _side_order(side: str) -> int:
    return {SIDE_FRONT: 0, SIDE_BACK: 1}.get(side, 2)


def classify_sheet(
    possible: list[tuple[str, ...]], required_sides: tuple[str, ...]
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...]]:
    """按可能进度数量分类。

    返回 (status_code, determined_state, pending_sides)：
    - ("pending", None, ())：多种可能进度 -> 待查；
    - ("orderable", state, pending)：进度唯一且仍有缺面 -> 可领单；
    - ("completed", state, ())：进度唯一且无缺面 -> 已补齐。
    """
    if len(possible) != 1:
        return "pending", None, ()
    state = possible[0]
    done = frozenset(state)
    pending = tuple(s for s in required_sides if s not in done)
    if pending:
        return "orderable", state, pending
    return "completed", state, ()


STATUS_CN = {
    "pending": "待查",
    "orderable": "可领单",
    "completed": "已补齐",
}
