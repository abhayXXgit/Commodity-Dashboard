"""
API integration tests against a real (temporary) database.

A throwaway SQLite file is seeded with a small slice of demo data, the app is
driven through FastAPI's TestClient, and the responses are checked for the
invariants that matter: correct arithmetic, honest data-class labelling, and
proper rejection of bad input.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# point the app at a scratch database BEFORE anything imports settings
_TMP = Path(tempfile.mkdtemp(prefix="procure-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["DATA_MODE"] = "DEMO"
os.environ["BOOTSTRAP_FORECASTS"] = "false"   # tests train explicitly, below


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from backend.database import init_db
    from backend.services.seed_data import run_seed
    from backend.main import app

    init_db()
    run_seed()
    # one short forecast run so the forecast endpoints have something to serve
    from backend.services.forecast_service import refresh_forecasts
    refresh_forecasts(series=[("CU", "BME", "CU_CATHODE", "Copper Cathode (India)")],
                      horizons=(7, 30, 90), folds=2)
    with TestClient(app) as c:
        yield c


class TestMeta:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_config_never_leaks_secrets(self, client):
        body = client.get("/api/config").text.lower()
        for forbidden in ("api_key", "apikey", "password", "secret", "token"):
            assert forbidden not in body

    def test_stats_reports_data_classes(self, client):
        j = client.get("/api/stats").json()
        assert j["price_rows"] > 1000
        assert "DEMO_DATA" in j["rows_by_data_class"]


class TestMarket:
    def test_masters(self, client):
        j = client.get("/api/market/masters").json()
        codes = {c["code"] for c in j["commodities"]}
        assert {"CU", "AL", "CRGO", "STEEL", "OIL"} <= codes
        assert j["headline"]["CU"]["provider"] == "BME"

    def test_kpis_carry_provenance(self, client):
        cards = [c for c in client.get("/api/market/kpis").json()["cards"] if c["available"]]
        assert cards
        for c in cards:
            if c.get("provider") == "AVERAGE":
                continue
            assert c["data_class"], "every KPI must declare its data class"
            assert c["source"] and c["as_of"]

    def test_live_endpoint_admits_there_is_no_feed(self, client):
        j = client.get("/api/market/live").json()
        assert j["live_available"] is False
        assert "unavailable" in (j["banner"] or "").lower()
        assert j["demo_data"] is True
        assert all(s["last_status"] in ("NEVER_RUN", "UNAVAILABLE", "FAILED", "SUCCESS")
                   for s in j["sources"])

    def test_series_returns_moving_averages(self, client):
        j = client.get("/api/market/series",
                       params={"commodity": "CU", "provider": "BME",
                               "product": "CU_CATHODE", "period": "365D"}).json()
        assert j["available"] and len(j["points"]) > 100
        assert j["moving_averages"]["ma30"] > 0
        assert j["band"]["zone"] in ("VERY LOW", "LOW", "NORMAL", "HIGH", "VERY HIGH")

    def test_unknown_series_is_404(self, client):
        r = client.get("/api/market/series", params={"commodity": "NOPE"})
        assert r.status_code == 404

    def test_provider_comparison_finds_the_cheapest(self, client):
        j = client.get("/api/market/providers/compare", params={"commodity": "AL"}).json()
        for t in j["table"]:
            prices = [e["price"] for e in t["entries"]]
            assert t["lowest"] == min(prices)
            assert t["spread"] == pytest.approx(max(prices) - min(prices), abs=0.01)
            lowest = [e for e in t["entries"] if e["is_lowest"]]
            assert len(lowest) == 1 and lowest[0]["price"] == min(prices)

    def test_copper_parity_arithmetic(self, client):
        j = client.get("/api/market/copper/parity").json()
        assert j["available"]
        # premium must equal india - lme equivalent
        assert j["premium"] == pytest.approx(
            j["india_price"] - j["lme_inr_equivalent"], abs=1.0)


class TestBom:
    def test_rollup_totals_are_internally_consistent(self, client):
        j = client.get("/api/bom/rollup").json()
        # sum of per-project material cost == portfolio material cost
        total = sum(t["project_material_cost"] for t in j["transformers"])
        assert total == pytest.approx(j["totals"]["current_material_cost"], rel=1e-6)
        # sum of per-commodity value == the same number
        by_c = sum(v["current_value"] for v in j["by_commodity"].values())
        assert by_c == pytest.approx(j["totals"]["current_material_cost"], rel=1e-6)

    def test_reference_transformer_matches_the_spec_example(self, client):
        j = client.get("/api/bom/transformers/PT-001").json()
        assert j["rating_mva"] == 10.0 and j["quantity"] == 5
        # 2.850 MT copper per transformer x 5 units
        assert j["copper_mt_project"] == pytest.approx(14.25, abs=0.01)
        assert j["aluminium_mt_project"] == pytest.approx(6.25, abs=0.01)

    def test_impact_is_change_times_consumption_times_quantity(self, client):
        j = client.get("/api/bom/impact").json()
        for r in j["rows"]:
            assert r["impact_per_transformer"] == pytest.approx(
                r["price_change"] * r["bom_mt_per_transformer"], rel=1e-4, abs=1.0)
            assert r["total_exposure"] == pytest.approx(
                r["price_change"] * r["total_requirement_mt"], rel=1e-4, abs=1.0)

    def test_what_if_arithmetic(self, client):
        r = client.post("/api/bom/what-if", json={
            "commodity": "CU", "scenario_price": 950_000, "current_price": 900_000,
            "consumption_mt": 2.85, "quantity": 20})
        j = r.json()
        assert j["price_change"] == 50_000
        assert j["impact_per_transformer"] == pytest.approx(142_500.0)
        assert j["total_project_impact"] == pytest.approx(2_850_000.0)

    def test_what_if_rejects_a_non_positive_price(self, client):
        assert client.post("/api/bom/what-if",
                           json={"commodity": "CU", "scenario_price": 0}).status_code == 422

    def test_sensitivity_grid_is_symmetric_around_current(self, client):
        j = client.get("/api/bom/sensitivity", params={"commodity": "CU"}).json()
        cur = [r for r in j["rows"] if r["is_current"]][0]
        assert cur["scenario_pct"] == 0
        assert cur["price"] == pytest.approx(j["current_price"], rel=1e-6)
        assert cur["project_exposure"] == pytest.approx(0.0, abs=1.0)
        up = [r for r in j["rows"] if r["scenario_pct"] == 10][0]
        assert up["price"] == pytest.approx(j["current_price"] * 1.1, rel=1e-6)

    def test_waterfall_closes(self, client):
        j = client.get("/api/bom/waterfall").json()
        total = sum(s["impact"] for s in j["steps"])
        assert j["revised_cost_per_transformer"] == pytest.approx(
            j["original_cost_per_transformer"] + total, rel=1e-6)

    def test_coverage_identity(self, client):
        for r in client.get("/api/bom/coverage").json()["rows"]:
            assert r["covered_mt"] == pytest.approx(
                r["stock_mt"] + r["open_po_mt"] + r["confirmed_mt"], abs=0.01)
            assert r["uncovered_mt"] == pytest.approx(
                max(r["requirement_mt"] - r["covered_mt"], 0), abs=0.01)

    def test_flow_chain_is_consistent(self, client):
        j = client.get("/api/bom/flow", params={"commodity": "CU"}).json()
        assert j["material_cost_per_transformer"] == pytest.approx(
            j["consumption_mt_per_transformer"] * j["price"], rel=1e-4)
        assert j["potential_exposure"] == pytest.approx(
            j["forecast_exposure"] - j["total_exposure"], rel=1e-4, abs=1.0)


class TestProcurement:
    def test_recommendation_shape_and_disclaimer(self, client):
        j = client.get("/api/procurement/recommendation", params={"commodity": "CU"}).json()
        assert j["signal"] in ["BUY NOW", "BUY PARTIAL", "LOCK PRICE", "PHASED PROCUREMENT",
                               "NEGOTIATE", "MONITOR", "WAIT", "DO NOT LOCK"]
        assert "not financial" in j["disclaimer"].lower()
        assert j["reasons"] and len(j["reasons"]) >= 3
        assert set(j["weights"]) == set(j["factors"])
        assert sum(j["weights"].values()) == pytest.approx(1.0)

    def test_summary_is_generated_not_hardcoded(self, client):
        j = client.get("/api/procurement/summary").json()
        assert len(j["sentences"]) >= 6
        # it must quote real current numbers, so digits are present throughout
        assert sum(any(ch.isdigit() for ch in s) for s in j["sentences"]) >= 5

    def test_summary_declares_demo_data(self, client):
        text = client.get("/api/procurement/summary").json()["summary"].lower()
        assert "demo data" in text

    def test_executive_view(self, client):
        j = client.get("/api/procurement/executive").json()
        assert j["overall_risk"] in ("LOW", "MEDIUM", "HIGH")
        assert j["total_exposure"] > 0
        assert j["potential_exposure_30d"] == pytest.approx(
            j["forecast_exposure"]["30d"] - j["total_exposure"], rel=1e-6)

    def test_price_lock_scenarios_are_ordered(self, client):
        j = client.post("/api/procurement/price-lock",
                        json={"commodity": "CU", "lock_period_days": 90}).json()
        prices = [s["price"] for s in j["scenarios"]]
        assert prices[0] < prices[1] < prices[2]      # best < base < worst

    def test_alerts_evaluate_is_idempotent(self, client):
        """Re-running the daily job must never duplicate an alert.

        This is a timezone trap: alerts are stored in UTC while the app runs in
        IST, and SQLite drops the tzinfo. A calendar-day window silently matched
        nothing either side of midnight and duplicated every alert on every run.
        """
        client.post("/api/procurement/alerts/evaluate")
        before = client.get("/api/procurement/alerts").json()["alerts"]
        second = client.post("/api/procurement/alerts/evaluate").json()["count"]
        after = client.get("/api/procurement/alerts").json()["alerts"]
        assert second == 0
        assert len(after) == len(before)


class TestQuotes:
    def test_quotes_are_benchmarked_against_their_own_product(self, client):
        for q in client.get("/api/quotes").json()["quotes"]:
            if q["variance_pct"] is None:
                continue
            # a sane quote is within a few percent of its own product benchmark
            assert abs(q["variance_pct"]) < 15, f"{q['supplier']} {q['product']}"
            assert q["landed_cost"] > q["effective_price"]   # tax + freight added

    def test_create_and_delete_quote(self, client):
        r = client.post("/api/quotes", json={
            "supplier": "Test Metals Pvt Ltd", "commodity": "AL", "product": "AL_INGOT",
            "price": 265000, "currency": "INR", "unit": "MT",
            "freight": 2000, "tax_pct": 18})
        assert r.status_code == 200
        q = r.json()["quote"]
        assert q["effective_price"] == pytest.approx(265000)
        assert q["landed_cost"] == pytest.approx((265000 + 2000) * 1.18, rel=1e-6)
        assert client.delete(f"/api/quotes/{q['id']}").status_code == 200

    def test_quote_in_foreign_unit_is_normalised(self, client):
        r = client.post("/api/quotes", json={
            "supplier": "Per-Kg Vendor", "commodity": "AL", "product": "AL_INGOT",
            "price": 265, "currency": "INR", "unit": "KG", "tax_pct": 0})
        q = r.json()["quote"]
        assert q["effective_price"] == pytest.approx(265_000)   # per-kg -> per-MT
        client.delete(f"/api/quotes/{q['id']}")

    def test_bad_commodity_rejected(self, client):
        r = client.post("/api/quotes", json={"supplier": "X", "commodity": "ZZZ", "price": 1})
        assert r.status_code == 400


class TestDataIO:
    def test_template_downloads(self, client):
        r = client.get("/api/data/template")
        assert r.status_code == 200 and len(r.content) > 4000

    def test_import_rejects_bad_rows_and_reports_why(self, client):
        import io
        import pandas as pd
        df = pd.DataFrame([
            {"Date": "2026-02-02", "Commodity": "AL", "Provider": "NALCO",
             "Product": "AL_INGOT", "Price": 261000, "Currency": "INR", "Unit": "MT"},
            {"Date": "2026-02-02", "Commodity": "AL", "Provider": "NALCO",
             "Product": "AL_INGOT", "Price": -1, "Currency": "INR", "Unit": "MT"},
            {"Date": "2026-02-02", "Commodity": "NOPE", "Provider": "NALCO",
             "Product": "AL_INGOT", "Price": 100, "Currency": "INR", "Unit": "MT"},
            {"Date": "2030-01-01", "Commodity": "AL", "Provider": "NALCO",
             "Product": "AL_INGOT", "Price": 100, "Currency": "INR", "Unit": "MT"},
        ])
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        r = client.post("/api/data/import?dry_run=true",
                        files={"file": ("t.xlsx", buf.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        j = r.json()
        assert j["ok"] and j["rows_read"] == 4
        assert j["rows_rejected"] == 3
        reasons = " ".join(x["reason"] for x in j["rejected"])
        assert "non-positive" in reasons and "unknown commodity" in reasons and "future" in reasons

    def test_verified_data_supersedes_demo_data(self, client):
        """
        The first real circular an operator imports must replace the simulated
        row for the same series and date. Matching on (date, commodity,
        provider, product) alone treats it as a duplicate, silently discards it,
        and leaves the demo price on screen with no sign anything went wrong.
        """
        import datetime as dt
        import io

        import pandas as pd

        day = dt.date.today().isoformat()

        def nalco_price():
            cards = client.get("/api/market/kpis").json()["cards"]
            c = next(x for x in cards if x.get("provider") == "NALCO"
                     and x.get("product") == "AL_INGOT")
            return c["current"], c["data_class"]

        before, before_class = nalco_price()
        assert before_class == "DEMO_DATA"

        buf = io.BytesIO()
        pd.DataFrame([{"Date": day, "Product": "Ingot", "Grade": "P1020A",
                       "Price": before + 4321, "Currency": "INR", "Unit": "MT",
                       "Basis": "EX_PLANT"}]).to_excel(buf, index=False)
        r = client.post("/api/data/import",
                        files={"file": ("nalco_official_circular.xlsx", buf.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        j = r.json()
        # commodity and provider are not columns in a producer's own circular
        assert j["inferred_from_filename"] == {"commodity": "AL", "provider": "NALCO"}
        assert j["rows_inserted"] == 1
        assert j["rows_superseded"] == 1

        after, after_class = nalco_price()
        assert after == before + 4321
        assert after_class == "VERIFIED_HISTORICAL"

    def test_demo_data_never_overwrites_verified_data(self, client):
        """The precedence rule must not work in reverse."""
        import datetime as dt
        import io

        import pandas as pd

        day = dt.date.today().isoformat()
        buf = io.BytesIO()
        pd.DataFrame([{"Date": day, "Product": "Ingot", "Price": 1,
                       "Currency": "INR", "Unit": "MT"}]).to_excel(buf, index=False)
        j = client.post("/api/data/import?data_class=DEMO_DATA",
                        files={"file": ("nalco_junk.xlsx", buf.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}).json()
        assert j["rows_inserted"] == 0
        assert j["duplicates_already_stored"] == 1

        cards = client.get("/api/market/kpis").json()["cards"]
        c = next(x for x in cards if x.get("provider") == "NALCO"
                 and x.get("product") == "AL_INGOT")
        assert c["data_class"] == "VERIFIED_HISTORICAL"
        assert c["current"] != 1

    def test_producer_circular_columns_are_inferred(self, client):
        from backend.services.excel_io import infer_from_filename, infer_product
        assert infer_from_filename("nalco_2026-09.xlsx") == ("AL", "NALCO")
        assert infer_from_filename("HINDALCO circular.xls") == ("AL", "HINDALCO")
        assert infer_from_filename("balco-wef-0109.csv") == ("AL", "BALCO")
        assert infer_from_filename("untitled.xlsx") == (None, None)
        assert infer_product("Wire Rod", "AL") == "AL_WIREROD"
        assert infer_product("P1020A Ingot", "AL") == "AL_INGOT"
        assert infer_product("Cathode", "CU") == "CU_CATHODE"

    def test_sample_files_cannot_become_real_prices(self, client):
        """
        A format demonstration must never be recorded as a published price.

        The shipped example circular was being auto-ingested and stamped
        "NALCO official price circular / VERIFIED_HISTORICAL" - invented figures
        entering as trusted data, which the data-class precedence rule would
        then rank above everything else.
        """
        import io

        import pandas as pd

        from backend.data_sources.circular_adapter import looks_like_a_sample

        assert looks_like_a_sample("nalco_circular_sample.xlsx")
        assert looks_like_a_sample("NALCO Example Sept.xlsx")
        assert looks_like_a_sample("balco_template.csv")
        assert looks_like_a_sample("hindalco-demo.xls")
        # a genuine circular must still be accepted
        assert not looks_like_a_sample("nalco_2026-09.xlsx")
        assert not looks_like_a_sample("HINDALCO wef 01092026.xlsx")

        buf = io.BytesIO()
        pd.DataFrame([{"Date": "2026-02-02", "Product": "Ingot", "Price": 999999,
                       "Currency": "INR", "Unit": "MT"}]).to_excel(buf, index=False)
        r = client.post("/api/data/import",
                        files={"file": ("nalco_sample.xlsx", buf.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 400
        assert "sample or template" in r.json()["detail"]

    def test_import_rejects_a_non_spreadsheet(self, client):
        r = client.post("/api/data/import",
                        files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
        assert r.status_code == 400

    def test_excel_export_is_a_workbook(self, client):
        r = client.get("/api/data/export/excel")
        assert r.status_code == 200 and r.content[:2] == b"PK"   # zip magic

    def test_csv_export_has_provenance_columns(self, client):
        head = client.get("/api/data/export/csv").text.splitlines()[0]
        for col in ("date", "price", "currency", "unit", "source", "data_class"):
            assert col in head


class TestForecastApi:
    def test_forecast_horizons_and_bands(self, client):
        j = client.get("/api/forecast", params={"commodity": "CU"}).json()
        assert j["available"]
        for r in j["results"]:
            assert r["lower_ci"] < r["forecast_price"] < r["upper_ci"]
            assert r["model_used"] and r["mape"] is not None
            assert r["confidence"] in ("HIGH", "MEDIUM", "LOW")
        # uncertainty must widen with horizon
        by_h = {r["horizon_days"]: r["upper_ci"] - r["lower_ci"] for r in j["results"]}
        if 7 in by_h and 90 in by_h:
            assert by_h[90] > by_h[7]

    def test_forecast_is_labelled_as_a_projection(self, client):
        j = client.get("/api/forecast", params={"commodity": "CU"}).json()
        assert "not a guarantee" in j["disclaimer"].lower()
