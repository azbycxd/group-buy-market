import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


MAX_OUT_TRADE_NO_LENGTH = 128
ALLOWED_OUT_TRADE_NO = re.compile(r"^[A-Za-z0-9._-]+$")


class OrderFactsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outTradeNo: StrictStr

    @field_validator("outTradeNo")
    @classmethod
    def out_trade_no_must_be_valid(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("OUT_TRADE_NO_MISSING")
        if len(normalized) > MAX_OUT_TRADE_NO_LENGTH:
            raise ValueError("OUT_TRADE_NO_INVALID_LENGTH")
        if not ALLOWED_OUT_TRADE_NO.fullmatch(normalized):
            raise ValueError("OUT_TRADE_NO_INVALID_CHARACTERS")
        return normalized


class ActivityIdArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activityId: StrictInt = Field(gt=0)
