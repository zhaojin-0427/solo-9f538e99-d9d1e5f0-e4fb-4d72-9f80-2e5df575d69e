"""请求 / 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _strip(value):
    return str(value).strip() if value is not None else value


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
