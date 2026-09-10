"""
SQLAlchemy ORM models — mirrors database/schema.sql.

Provenance rule enforced across every price entity:
    original price + currency + unit are stored verbatim;
    `price_inr_mt` is the derived analytics value and may be recomputed.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Index,
                        Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from backend.database import Base


def _now():
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- masters
class Commodity(Base):
    __tablename__ = "commodity_master"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    category = Column(String(64))
    base_unit = Column(String(16), default="MT", nullable=False)
    base_currency = Column(String(8), default="INR", nullable=False)
    benchmark_source = Column(String(128))
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    products = relationship("Product", back_populates="commodity", cascade="all, delete-orphan")


class Provider(Base):
    __tablename__ = "provider_master"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    provider_type = Column(String(32), nullable=False)
    country = Column(String(64))
    default_currency = Column(String(8), default="INR", nullable=False)
    default_unit = Column(String(16), default="MT", nullable=False)
    default_basis = Column(String(64))
    source_url = Column(Text)
    ingestion_mode = Column(String(32), default="MANUAL")
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)


class Product(Base):
    __tablename__ = "product_master"
    id = Column(Integer, primary_key=True)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(48), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    grade = Column(String(64))
    specification = Column(Text)
    default_unit = Column(String(16), default="MT", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    commodity = relationship("Commodity", back_populates="products")


# --------------------------------------------------------------------------- prices
class HistoricalPrice(Base):
    __tablename__ = "historical_prices"
    id = Column(Integer, primary_key=True)
    price_date = Column(Date, nullable=False, index=True)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_master.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("product_master.id"))
    grade = Column(String(64))

    price = Column(Numeric(18, 4), nullable=False)
    currency = Column(String(8), nullable=False)
    unit = Column(String(16), nullable=False)
    open_price = Column(Numeric(18, 4))
    high_price = Column(Numeric(18, 4))
    low_price = Column(Numeric(18, 4))
    close_price = Column(Numeric(18, 4))

    price_basis = Column(String(64))
    location = Column(String(128))
    premium = Column(Numeric(18, 4), default=0)
    freight = Column(Numeric(18, 4), default=0)
    tax_pct = Column(Numeric(9, 4), default=0)

    price_inr_mt = Column(Numeric(18, 4))
    fx_rate_used = Column(Numeric(18, 6))

    effective_date = Column(Date)
    source = Column(String(128), nullable=False)
    source_url = Column(Text)
    data_class = Column(String(32), default="VERIFIED_HISTORICAL", nullable=False)
    validation_status = Column(String(32), default="PENDING", nullable=False)
    quality_flags = Column(Text)
    revision = Column(Integer, default=1, nullable=False)
    data_timestamp = Column(DateTime(timezone=True), default=_now, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("price_date", "commodity_id", "provider_id", "product_id",
                         "grade", "revision", name="uq_hist"),
        Index("ix_hist_series", "commodity_id", "provider_id", "product_id", "price_date"),
    )


class LivePrice(Base):
    __tablename__ = "live_prices"
    id = Column(Integer, primary_key=True)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_master.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("product_master.id"))
    latest_price = Column(Numeric(18, 4), nullable=False)
    previous_price = Column(Numeric(18, 4))
    currency = Column(String(8), nullable=False)
    unit = Column(String(16), nullable=False)
    price_inr_mt = Column(Numeric(18, 4))
    day_change = Column(Numeric(18, 4))
    day_change_pct = Column(Numeric(12, 4))
    week_change_pct = Column(Numeric(12, 4))
    month_change_pct = Column(Numeric(12, 4))
    ytd_change_pct = Column(Numeric(12, 4))
    price_basis = Column(String(64))
    source = Column(String(128), nullable=False)
    source_url = Column(Text)
    data_class = Column(String(32), default="LIVE", nullable=False)
    confidence = Column(String(32), default="UNKNOWN", nullable=False)
    is_stale = Column(Boolean, default=False, nullable=False)
    stale_reason = Column(Text)
    last_updated = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (UniqueConstraint("commodity_id", "provider_id", "product_id", name="uq_live"),)


class SupplierQuote(Base):
    __tablename__ = "supplier_quotes"
    id = Column(Integer, primary_key=True)
    quote_ref = Column(String(64))
    supplier_name = Column(String(160), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_master.id"))
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("product_master.id"))
    quote_date = Column(Date, nullable=False)
    valid_until = Column(Date)
    price = Column(Numeric(18, 4), nullable=False)
    currency = Column(String(8), default="INR", nullable=False)
    unit = Column(String(16), default="MT", nullable=False)
    freight = Column(Numeric(18, 4), default=0)
    premium = Column(Numeric(18, 4), default=0)
    tax_pct = Column(Numeric(9, 4), default=18)
    landed_cost_inr_mt = Column(Numeric(18, 4))
    effective_price_inr_mt = Column(Numeric(18, 4))
    variance_vs_market = Column(Numeric(18, 4))
    variance_pct = Column(Numeric(12, 4))
    variance_vs_prev = Column(Numeric(18, 4))
    status_flag = Column(String(32))
    moq_mt = Column(Numeric(14, 4))
    payment_terms = Column(String(160))
    delivery_days = Column(Integer)
    price_basis = Column(String(64))
    remarks = Column(Text)
    data_class = Column(String(32), default="SUPPLIER_QUOTE", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    id = Column(Integer, primary_key=True)
    rate_date = Column(Date, nullable=False)
    base_currency = Column(String(8), nullable=False)
    quote_currency = Column(String(8), nullable=False)
    rate = Column(Numeric(18, 6), nullable=False)
    source = Column(String(128), nullable=False)
    data_class = Column(String(32), default="VERIFIED_HISTORICAL", nullable=False)
    data_timestamp = Column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("rate_date", "base_currency", "quote_currency", name="uq_fx"),)


class FreightPremium(Base):
    __tablename__ = "freight_premium"
    id = Column(Integer, primary_key=True)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_master.id"))
    location = Column(String(128))
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date)
    premium_inr_mt = Column(Numeric(18, 4), default=0)
    freight_inr_mt = Column(Numeric(18, 4), default=0)
    remarks = Column(Text)


class Forecast(Base):
    __tablename__ = "forecasts"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), nullable=False)
    generated_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("provider_master.id"))
    product_id = Column(Integer, ForeignKey("product_master.id"))
    horizon_days = Column(Integer, nullable=False)
    target_date = Column(Date, nullable=False)
    forecast_price = Column(Numeric(18, 4), nullable=False)
    lower_ci = Column(Numeric(18, 4))
    upper_ci = Column(Numeric(18, 4))
    model_used = Column(String(64), nullable=False)
    mae = Column(Numeric(18, 4))
    rmse = Column(Numeric(18, 4))
    mape = Column(Numeric(12, 4))
    direction = Column(String(16))
    confidence = Column(String(16))
    data_class = Column(String(32), default="FORECAST", nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "commodity_id", "provider_id", "product_id",
                         "horizon_days", name="uq_forecast"),
    )


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    alert_code = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"))
    provider_id = Column(Integer, ForeignKey("provider_master.id"))
    product_id = Column(Integer, ForeignKey("product_master.id"))
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    metric_value = Column(Numeric(18, 4))
    threshold_value = Column(Numeric(18, 4))
    triggered_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_at = Column(DateTime(timezone=True))


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id = Column(Integer, primary_key=True)
    alert_code = Column(String(64), unique=True, nullable=False)
    description = Column(Text)
    rule_type = Column(String(48), nullable=False)
    commodity_code = Column(String(32))
    threshold = Column(Numeric(18, 4), nullable=False)
    direction = Column(String(16), default="BOTH")
    severity = Column(String(16), default="WARNING", nullable=False)
    is_enabled = Column(Boolean, default=True, nullable=False)


class DataSourceLog(Base):
    __tablename__ = "data_source_log"
    id = Column(Integer, primary_key=True)
    source_name = Column(String(128), nullable=False)
    adapter = Column(String(128))
    run_started = Column(DateTime(timezone=True), default=_now, nullable=False)
    run_finished = Column(DateTime(timezone=True))
    status = Column(String(32), nullable=False)
    rows_fetched = Column(Integer, default=0)
    rows_inserted = Column(Integer, default=0)
    rows_rejected = Column(Integer, default=0)
    duplicates_found = Column(Integer, default=0)
    http_status = Column(Integer)
    error_message = Column(Text)
    last_success_at = Column(DateTime(timezone=True))


class DataQualityFlag(Base):
    __tablename__ = "data_quality_flags"
    id = Column(Integer, primary_key=True)
    table_name = Column(String(64), nullable=False)
    record_id = Column(Integer)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"))
    flag_type = Column(String(48), nullable=False)
    detail = Column(Text)
    severity = Column(String(16), default="WARNING", nullable=False)
    detected_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    resolved = Column(Boolean, default=False, nullable=False)


class UserSetting(Base):
    __tablename__ = "user_settings"
    id = Column(Integer, primary_key=True)
    setting_key = Column(String(96), unique=True, nullable=False)
    setting_value = Column(Text)
    value_type = Column(String(16), default="string", nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


# --------------------------------------------------------------------------- transformer / BOM
class Transformer(Base):
    __tablename__ = "transformer_master"
    id = Column(Integer, primary_key=True)
    job_no = Column(String(48), unique=True, nullable=False)
    customer = Column(String(160))
    transformer_type = Column(String(64))
    rating_mva = Column(Numeric(10, 3), nullable=False)
    voltage_ratio = Column(String(48))
    hv_kv = Column(Numeric(10, 3))
    lv_kv = Column(Numeric(10, 3))
    frequency_hz = Column(Numeric(6, 2), default=50)
    vector_group = Column(String(32))
    quantity = Column(Integer, default=1, nullable=False)
    delivery_date = Column(Date)
    mfg_status = Column(String(48))
    costing_version = Column(String(32))
    bom_version = Column(String(32))
    base_cost_inr = Column(Numeric(18, 2))
    created_at = Column(DateTime(timezone=True), default=_now)

    bom_items = relationship("BomItem", back_populates="transformer", cascade="all, delete-orphan")


class BomItem(Base):
    __tablename__ = "transformer_bom"
    id = Column(Integer, primary_key=True)
    transformer_id = Column(Integer, ForeignKey("transformer_master.id", ondelete="CASCADE"), nullable=False)
    bom_item = Column(String(160), nullable=False)
    material_category = Column(String(48), nullable=False)
    material = Column(String(160))
    grade = Column(String(64))
    specification = Column(Text)
    quantity = Column(Numeric(18, 4), nullable=False)
    unit = Column(String(16), default="KG", nullable=False)
    unit_weight_kg = Column(Numeric(18, 4))
    total_weight_mt = Column(Numeric(18, 6), nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"))
    provider_id = Column(Integer, ForeignKey("provider_master.id"))
    product_id = Column(Integer, ForeignKey("product_master.id"))
    price_basis = Column(String(64))
    bom_rate_inr_mt = Column(Numeric(18, 4))
    premium_inr_mt = Column(Numeric(18, 4), default=0)
    currency = Column(String(8), default="INR")
    supplier = Column(String(160))
    po_number = Column(String(64))
    remarks = Column(Text)

    transformer = relationship("Transformer", back_populates="bom_items")


class ProcurementCoverage(Base):
    __tablename__ = "procurement_coverage"
    id = Column(Integer, primary_key=True)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    transformer_id = Column(Integer, ForeignKey("transformer_master.id"))
    as_of_date = Column(Date, default=dt.date.today, nullable=False)
    stock_mt = Column(Numeric(18, 4), default=0)
    open_po_mt = Column(Numeric(18, 4), default=0)
    confirmed_mt = Column(Numeric(18, 4), default=0)
    avg_po_rate_inr_mt = Column(Numeric(18, 4))
    supplier_lead_days = Column(Integer)
    remarks = Column(Text)


class PurchaseHistory(Base):
    __tablename__ = "purchase_history"
    id = Column(Integer, primary_key=True)
    po_number = Column(String(64))
    po_date = Column(Date, nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("product_master.id"))
    supplier_name = Column(String(160))
    quantity_mt = Column(Numeric(18, 4), nullable=False)
    rate_inr_mt = Column(Numeric(18, 4), nullable=False)
    basis = Column(String(64))
    transformer_id = Column(Integer, ForeignKey("transformer_master.id"))
    data_class = Column(String(32), default="VERIFIED_HISTORICAL", nullable=False)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id = Column(Integer, primary_key=True)
    generated_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    commodity_id = Column(Integer, ForeignKey("commodity_master.id"), nullable=False)
    signal = Column(String(32), nullable=False)
    confidence = Column(String(16))
    score = Column(Numeric(10, 4))
    suggested_cover_pct = Column(Numeric(6, 2))
    rationale = Column(Text, nullable=False)
    inputs_json = Column(Text)
