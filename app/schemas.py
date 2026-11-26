"""请求 / 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _strip(value):
    return str(value).strip() if value is not None else value


def _as_int(value):
    """尽量把输入转成 int；布尔、数字字符串都接受。"""
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text == "":
        return None
    return int(text)  # 无法转换时抛 ValueError -> 422


def _first_key(data: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


_FRONT_KEYS = (
    "front_success", "front_success_count", "front_success_sheets",
    "front_ok", "front_ok_count", "front_count", "front_total",
    "front_printed", "front_printed_count", "front_reprint_success",
    "front_reprinted", "front_repaired", "front", "zheng_mian",
    "正面补印成功张数", "正面成功张数", "正面成功", "正面张数", "正面",
)
_BACK_KEYS = (
    "back_success", "back_success_count", "back_success_sheets",
    "back_ok", "back_ok_count", "back_count", "back_total",
    "back_printed", "back_printed_count", "back_reprint_success",
    "back_reprinted", "back_repaired", "back", "fan_mian",
    "反面补印成功张数", "反面成功张数", "反面成功", "反面张数", "反面",
)
_SHEET_LIST_KEYS = (
    "sheet_nos", "sheet_numbers", "sheet_no_list", "sheets",
    "on_press_sheets", "pressed_sheets", "press_sheets", "onpress_sheets",
    "machine_sheets", "press_sheet_nos", "selected_sheets",
    "本轮上机纸号", "上机纸号", "纸号列表", "纸号",
)
_NESTED_COUNT_KEYS = (
    "counts", "success_counts", "machine_counts", "press_counts",
    "printed_counts", "success", "totals", "张数",
)


class SignatureIn(BaseModel):
    signature_no: str = Field(min_length=1, max_length=32, description="帖号", examples=["S01"])
    front_plate: str = Field(min_length=1, max_length=64, description="正面版号", examples=["A-01"])
    back_plate: str = Field(min_length=1, max_length=64, description="反面版号", examples=["A-02"])

    _strip_fields = field_validator("signature_no", "front_plate", "back_plate", mode="before")(_strip)


class SheetIn(BaseModel):
    sheet_no: str = Field(min_length=1, max_length=32, description="纸号", examples=["P001"])
    printed_plate: str | None = Field(
        default=None, max_length=64, description="已印版号；空白纸留空", examples=[None]
    )

    _strip_no = field_validator("sheet_no", mode="before")(_strip)

    @field_validator("printed_plate", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        """空白纸的版号留空：空串、纯空白都按 NULL 处理。"""
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class BatchCreate(BaseModel):
    signatures: list[SignatureIn] = Field(min_length=1, max_length=24, description="书帖，至多 24 帖")
    sheets: list[SheetIn] = Field(min_length=1, max_length=48, description="纸张，至多 48 张")


class ClaimIn(BaseModel):
    signature_no: str = Field(min_length=1, max_length=32, description="帖号", examples=["S01"])
    sheet_no: str = Field(min_length=1, max_length=32, description="纸号", examples=["P001"])

    _strip_fields = field_validator("signature_no", "sheet_no", mode="before")(_strip)


class PrintJob(BaseModel):
    side: str = Field(description="待印面：正面 / 反面")
    plate: str = Field(description="版号")


class InstructionOut(BaseModel):
    batch_id: int
    signature_no: str = Field(description="帖号")
    sheet_no: str = Field(description="纸号")
    prints: list[PrintJob] = Field(description="待印面与版号清单")
    replayed: bool = Field(description="true 表示同帖同纸重试，返回的是原指令")


class BatchOut(BaseModel):
    batch_id: int
    created_at: str
    signature_count: int
    sheet_count: int


# ---------------------------------------------------------------------------
# 急停 / 报验 / 续印单
#
# 这些模型对字段名做了宽松兼容：参考实现或调用方可能使用近义命名，
# before 校验器统一归并到规范字段；响应里则会同时给出多组近义键。
# ---------------------------------------------------------------------------


class StopIn(BaseModel):
    """记录一次急停：本轮上机纸号 + 印机正/反面补印成功张数。每批一次。

    sheet_nos 省略时由服务端默认取本批全部已领纸。
    """

    model_config = ConfigDict(extra="ignore")

    sheet_nos: list[str] | None = Field(default=None, max_length=48, description="本轮上机的同批已领纸号；省略=全部已领纸")
    front_success: int = Field(default=0, ge=0, le=100000, description="印机记录：正面补印成功张数")
    back_success: int = Field(default=0, ge=0, le=100000, description="印机记录：反面补印成功张数")

    @model_validator(mode="before")
    @classmethod
    def _compat_names(cls, data):
        if not isinstance(data, dict):
            return data
        d = dict(data)

        nested = _first_key(d, _NESTED_COUNT_KEYS)
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update({k: v for k, v in d.items() if k not in _NESTED_COUNT_KEYS})
            d = merged

        raw_sheets = _first_key(d, _SHEET_LIST_KEYS)
        summed_front = summed_back = 0
        have_per_sheet_counts = False
        if raw_sheets is not None:
            if isinstance(raw_sheets, str):
                raw_sheets = [p for p in raw_sheets.replace("，", ",").split(",") if p.strip()]
            nos: list[str] = []
            for item in raw_sheets:
                if isinstance(item, dict):
                    no = _first_key(item, ("sheet_no", "sheet_num", "sheet_number",
                                           "paper_no", "no", "id", "纸号"))
                    if no is not None:
                        nos.append(_strip(no))
                    fv = _first_key(item, _FRONT_KEYS)
                    bv = _first_key(item, _BACK_KEYS)
                    if fv is not None or bv is not None:
                        have_per_sheet_counts = True
                        if fv is not None:
                            summed_front += _as_int(fv) or 0
                        if bv is not None:
                            summed_back += _as_int(bv) or 0
                else:
                    nos.append(_strip(item))
            d["sheet_nos"] = [n for n in nos if n]

        front_raw = _first_key(d, _FRONT_KEYS)
        if isinstance(front_raw, dict):
            front_raw = _first_key(front_raw, ("success", "count", "value", "n", "张数"))
        back_raw = _first_key(d, _BACK_KEYS)
        if isinstance(back_raw, dict):
            back_raw = _first_key(back_raw, ("success", "count", "value", "n", "张数"))
        if front_raw is not None:
            d["front_success"] = _as_int(front_raw)
        elif have_per_sheet_counts:
            d["front_success"] = summed_front
        if back_raw is not None:
            d["back_success"] = _as_int(back_raw)
        elif have_per_sheet_counts:
            d["back_success"] = summed_back
        return d

    @field_validator("sheet_nos")
    @classmethod
    def _strip_each(cls, value):
        return [str(v).strip() for v in value]


def _truthy_side_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "ok", "done", "已补", "是", "对"}


def _extract_side_entries(raw) -> list:
    """从 list/dict/str 等多种报验形态里抽出包含面名的原始条目。"""
    entries: list = []
    if raw is None:
        return entries
    if isinstance(raw, str):
        entries.append(raw)
    elif isinstance(raw, dict):
        # {"正面": true, "反面": false} 或 {"side": "正面", "done": true}
        if "side" in raw or "面" in raw:
            flag = raw.get("done", raw.get("success", raw.get("ok", True)))
            if _truthy_side_flag(flag):
                entries.append(raw.get("side", raw.get("面")))
        else:
            for key, val in raw.items():
                if val is not False and not (isinstance(val, int) and val == 0):
                    entries.append(key)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                side = item.get("side", item.get("name", item.get("面", item.get("side_name"))))
                flag = item.get("done", item.get("completed", item.get("success",
                               item.get("ok", item.get("printed", True)))))
                if side is not None and _truthy_side_flag(flag):
                    entries.append(side)
            else:
                entries.append(item)
    return entries


class InspectionIn(BaseModel):
    """按纸号报验 / 更正停机时已补的面。sides 为该纸完整的已补面集合。"""

    model_config = ConfigDict(extra="ignore")

    sheet_no: str | None = Field(default=None, max_length=32, description="纸号（路径里给了可省略）")
    sides: list[str] = Field(default_factory=list, description="停机时已补成功的面：正面/反面")
    all_sides: bool = Field(default=False, description="报验为全部应补面都已补")

    @model_validator(mode="before")
    @classmethod
    def _compat_names(cls, data):
        if not isinstance(data, dict):
            return data
        from .core import normalize_side

        d = dict(data)

        sheet = _first_key(d, (
            "sheet_no", "sheet_num", "sheet_number", "paper_no", "paper_number",
            "no", "id", "纸号",
        ))
        if sheet is not None:
            d["sheet_no"] = _strip(sheet)

        def strict_list(raw) -> list[str]:
            # 规范 sides 字段：严格校验，无法识别的面名直接 422
            if isinstance(raw, str):
                parts = [p for p in raw.replace("，", ",").replace("、", ",").replace(" ", ",").split(",") if p]
            else:
                parts = list(raw)
            out: list[str] = []
            for item in parts:
                token = item.get("side") if isinstance(item, dict) else item
                side = normalize_side(token)  # ValueError -> 422
                if side not in out:
                    out.append(side)
            return out

        # 1) 调用方直接给了规范 sides：严格采用
        if d.get("sides") is not None:
            d["sides"] = strict_list(d["sides"])
        else:
            # 2) 其它近义字段名：尽力解析，跳过无法识别的标记
            entries: list = []
            structured_keys = (
                "completed_sides", "printed_sides", "done_sides",
                "reprinted_sides", "supplemented_sides", "finished_sides",
                "success_sides", "reported_sides", "inspected_sides",
                "results", "items", "reports",
                "已补面", "已补的面", "报验面",
            )
            for key in structured_keys:
                if d.get(key) is not None:
                    entries.extend(_extract_side_entries(d[key]))
            # 泛化的 progress/state：只接受列表/字典结构，避免把 "done" 之类误当面名
            for key in ("progress", "state", "states"):
                val = d.get(key)
                if isinstance(val, (list, tuple, dict)):
                    entries.extend(_extract_side_entries(val))
            for key in ("side", "completed_side", "reported_side"):
                if d.get(key) is not None:
                    # {"side": "正面", "completed": false} 形态：尊重同级否定标记
                    sibling_flag = _first_key(d, ("done", "completed", "success", "ok"))
                    if sibling_flag is not None and not _truthy_side_flag(sibling_flag):
                        continue
                    entries.extend(_extract_side_entries(d[key]))

            sides: list[str] = []
            all_marker = False
            for entry in entries:
                if isinstance(entry, str) and entry.strip().lower() in {
                    "all", "both", "completed", "complete", "done", "finished",
                    "full", "全部", "都补了", "两面", "正反面",
                }:
                    all_marker = True
                    continue
                try:
                    side = normalize_side(entry)
                except ValueError:
                    continue
                if side not in sides:
                    sides.append(side)
            d["sides"] = sides
            if all_marker:
                d["all_sides"] = True

        # 布尔/0-1 标量形式：front_completed=true、back_ok=1、{"正面": true}
        for keys, side_name in ((
            ("front_completed", "front_done", "front_ok", "front_success",
             "front_printed", "front", "is_front_done", "正面已补", "正面"),
            "正面",
        ), (
            ("back_completed", "back_done", "back_ok", "back_success",
             "back_printed", "back", "is_back_done", "反面已补", "反面",
             "背面已补", "背面"),
            "反面",
        )):
            value = _first_key(d, keys)
            if value is not None and not isinstance(value, (dict, list)) and _truthy_side_flag(value):
                if side_name not in d["sides"]:
                    d["sides"].append(side_name)

        all_done = _first_key(d, (
            "all_sides", "all_completed", "fully_completed", "complete_all",
            "all_done", "fully_printed", "completed_all", "全部已补", "已补齐",
        ))
        completed_flag = d.get("completed")
        if (isinstance(completed_flag, bool) and completed_flag) or (
            all_done is not None and _truthy_side_flag(all_done)
        ):
            d["all_sides"] = True
        return d

    @field_validator("sheet_no")
    @classmethod
    def _strip_sheet(cls, value):
        return _strip(value)

    @field_validator("sides")
    @classmethod
    def _order_sides(cls, value):
        # 入校验器已归一；这里仅按 正面->反面 固定顺序输出
        order = {"正面": 0, "反面": 1}
        return sorted(value, key=lambda s: order.get(s, 2))


class OrderClaimIn(BaseModel):
    """按纸号领取续印单。集合路由必须给 sheet_no；路径路由可省略。"""

    model_config = ConfigDict(extra="ignore")

    sheet_no: str | None = Field(default=None, max_length=32, description="纸号")

    @model_validator(mode="before")
    @classmethod
    def _compat_names(cls, data):
        if not isinstance(data, dict):
            return data
        d = dict(data)
        sheet = _first_key(d, (
            "sheet_no", "sheet_num", "sheet_number", "paper_no", "paper_number",
            "no", "id", "纸号",
        ))
        if sheet is not None:
            d["sheet_no"] = _strip(sheet)
        return d

    @field_validator("sheet_no")
    @classmethod
    def _strip_sheet(cls, value):
        return _strip(value)
