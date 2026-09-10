"""Pydantic request/response models for the public API."""
from __future__ import annotations


from pydantic import BaseModel, Field, field_validator


class QuoteIn(BaseModel):
    supplier: str = Field(min_length=1, max_length=160)
    commodity: str = Field(min_length=1, max_length=32)
    product: str | None = None
    quote_ref: str | None = None
    quote_date: str | None = None
    valid_until: str | None = None
    price: float = Field(gt=0)
    currency: str = "INR"
    unit: str = "MT"
    freight: float = 0
    premium: float = 0
    tax_pct: float = 18
    moq_mt: float | None = None
    payment_terms: str | None = None
    delivery_days: int | None = None
    price_basis: str = "EX_PLANT"
    remarks: str | None = None

    @field_validator("currency", "unit", "commodity")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


class WhatIfIn(BaseModel):
    commodity: str
    scenario_price: float = Field(gt=0)
    current_price: float | None = Field(default=None, gt=0)
    consumption_mt: float | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=0)

    @field_validator("commodity")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


class LockIn(BaseModel):
    commodity: str
    quantity_mt: float | None = Field(default=None, ge=0)
    lock_period_days: int = Field(default=90, ge=1, le=730)
    supplier_premium: float = 0


class ImpactIn(BaseModel):
    changes: dict[str, float] | None = None
    jobs: list[str] | None = None


class SettingIn(BaseModel):
    key: str
    value: str
    value_type: str = "string"


class TransformerIn(BaseModel):
    job_no: str
    customer: str | None = None
    transformer_type: str = "POWER"
    rating_mva: float = Field(gt=0)
    voltage_ratio: str | None = None
    quantity: int = Field(default=1, ge=1)
    delivery_date: str | None = None
    mfg_status: str = "PLANNED"
    base_cost_inr: float | None = None


class BomItemIn(BaseModel):
    bom_item: str
    material_category: str
    commodity: str | None = None
    provider: str | None = None
    product: str | None = None
    quantity: float = Field(gt=0)
    unit: str = "KG"
    grade: str | None = None
    bom_rate_inr_mt: float | None = None
    supplier: str | None = None
