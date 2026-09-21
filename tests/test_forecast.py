"""等待时间预测（/forecast）测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.errors import RequestValidationErrors
from app.forecast import forecast
from app.main import app
from app.models import ForecastRequest

client = TestClient(app)


def _run(now, buckets, cost, chain):
    return forecast(
        ForecastRequest.model_validate(
            {"now": now, "buckets": buckets, "cost": cost, "chain": chain}
        )
    )


def test_already_satisfied_wait_zero():
    resp = _run(10, [{"id": "A", "capacity": 10, "refill_rate": 1, "balance": 8}], 5, ["A"])
    assert resp.reachable is True
    assert resp.wait_seconds == 0
    assert resp.available_at == 10
    assert resp.limited_by == "A"
    assert resp.unreachable_buckets == []
    view = resp.buckets["A"]
    assert view.balance_now == 8
    assert view.reachable is True
    assert view.wait_seconds == 0
    assert view.projected_balance == 8


def test_single_bucket_wait_and_projection():
    # 余额 0、速率 2/s、成本 5 -> 等待 2.5 秒，届时余额恰好 5
    resp = _run(0, [{"id": "A", "capacity": 100, "refill_rate": 2, "balance": 0}], 5, ["A"])
    assert resp.reachable is True
    assert resp.wait_seconds == 2.5
    assert resp.available_at == 2.5
    assert resp.limited_by == "A"
    assert resp.buckets["A"].wait_seconds == 2.5
    assert resp.buckets["A"].projected_balance == 5


def test_limiting_bucket_is_longest_wait_and_projection_capped():
    # A 需等 5 秒（限制桶）；B 只需 1.5 秒，5 秒时早已补满到容量 10
    resp = _run(
        100,
        [
            {"id": "A", "capacity": 100, "refill_rate": 1, "balance": 0},
            {"id": "B", "capacity": 10, "refill_rate": 2, "balance": 2},
        ],
        5,
        ["A", "B"],
    )
    assert resp.reachable is True
    assert resp.wait_seconds == 5
    assert resp.available_at == 105
    assert resp.limited_by == "A"
    assert resp.buckets["A"].projected_balance == 5
    assert resp.buckets["B"].wait_seconds == 1.5
    assert resp.buckets["B"].projected_balance == 10


def test_zero_rate_insufficient_balance_unreachable():
    resp = _run(0, [{"id": "A", "capacity": 10, "refill_rate": 0, "balance": 3}], 5, ["A"])
    assert resp.reachable is False
    assert resp.wait_seconds is None
    assert resp.available_at is None
    assert resp.limited_by is None
    assert resp.unreachable_buckets == ["A"]
    view = resp.buckets["A"]
    assert view.reachable is False
    assert view.wait_seconds is None
    assert view.projected_balance is None
    assert view.balance_now == 3


def test_capacity_below_cost_unreachable_even_with_refill():
    resp = _run(0, [{"id": "A", "capacity": 4, "refill_rate": 100, "balance": 4}], 5, ["A"])
    assert resp.reachable is False
    assert resp.unreachable_buckets == ["A"]
    assert resp.wait_seconds is None
    assert resp.available_at is None


def test_zero_rate_with_sufficient_balance_is_immediate():
    resp = _run(7, [{"id": "A", "capacity": 10, "refill_rate": 0, "balance": 10}], 10, ["A"])
    assert resp.reachable is True
    assert resp.wait_seconds == 0
    assert resp.available_at == 7
    assert resp.buckets["A"].projected_balance == 10


def test_mixed_chain_lists_all_unreachable_buckets_in_chain_order():
    # A 可达（等 5 秒）；B 零速率且余额不足；C 容量小于成本
    resp = _run(
        0,
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0, "balance": 1},
            {"id": "C", "capacity": 3, "refill_rate": 2, "balance": 3},
        ],
        5,
        ["A", "B", "C"],
    )
    assert resp.reachable is False
    assert resp.unreachable_buckets == ["B", "C"]
    assert resp.wait_seconds is None
    assert resp.available_at is None
    assert resp.limited_by is None
    # 可达桶仍给出自身等待，但整体不可达时没有预计余额
    assert resp.buckets["A"].reachable is True
    assert resp.buckets["A"].wait_seconds == 5
    assert resp.buckets["A"].projected_balance is None
    assert resp.buckets["B"].reachable is False
    assert resp.buckets["C"].reachable is False


def test_balance_defaults_to_capacity():
    resp = _run(0, [{"id": "A", "capacity": 10, "refill_rate": 0}], 10, ["A"])
    assert resp.reachable is True
    assert resp.wait_seconds == 0
    assert resp.buckets["A"].balance_now == 10


def test_tie_breaks_to_first_bucket_in_chain_order():
    # A: (6-5)/1 = 1 秒；B: (6-4)/2 = 1 秒，等待并列
    buckets = [
        {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 5},
        {"id": "B", "capacity": 10, "refill_rate": 2, "balance": 4},
    ]
    assert _run(0, buckets, 6, ["A", "B"]).limited_by == "A"
    assert _run(0, buckets, 6, ["B", "A"]).limited_by == "B"


def test_absolute_now_passthrough():
    # 大时间原点（如 Unix 秒）直接平移：available_at = now + 等待
    resp = _run(
        1_700_000_000,
        [{"id": "A", "capacity": 100, "refill_rate": 4, "balance": 0}],
        10,
        ["A"],
    )
    assert resp.wait_seconds == 2.5
    assert resp.available_at == 1_700_000_002.5


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        ForecastRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "now": 0,
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0},
            ],
            "cost": 1,
            "chain": ["A"],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "now": 0,
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "cost": 1,
            "chain": ["A", "X"],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_non_positive_cost_rejected():
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "cost": 0,
                "chain": ["A"],
            }
        )


def test_balance_above_capacity_rejected():
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "balance": 6}],
                "cost": 1,
                "chain": ["A"],
            }
        )


def test_duplicate_bucket_within_chain_rejected():
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0},
                    {"id": "B", "capacity": 1, "refill_rate": 0},
                ],
                "cost": 1,
                "chain": ["A", "B", "A"],
            }
        )


# ---------- HTTP ----------

def test_http_forecast_ok():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "buckets": [
                {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "balance": 40},
                {"id": "api_qps", "capacity": 5, "refill_rate": 5, "balance": 1},
                {"id": "user_quota", "capacity": 20, "refill_rate": 1, "balance": 0},
            ],
            "cost": 4,
            "chain": ["tenant_quota", "api_qps", "user_quota"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is True
    assert data["wait_seconds"] == 4
    assert data["available_at"] == 4
    assert data["limited_by"] == "user_quota"
    assert data["unreachable_buckets"] == []
    # 预计可通过时刻的余额：tenant 补到 80，api_qps 触顶 5，user_quota 恰好 4
    assert data["buckets"]["tenant_quota"]["projected_balance"] == 80
    assert data["buckets"]["api_qps"]["projected_balance"] == 5
    assert data["buckets"]["user_quota"]["projected_balance"] == 4


def test_http_forecast_unreachable():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0, "balance": 2}],
            "cost": 5,
            "chain": ["A"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is False
    assert data["wait_seconds"] is None
    assert data["available_at"] is None
    assert data["limited_by"] is None
    assert data["unreachable_buckets"] == ["A"]


def test_http_forecast_validation_error_is_422():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "cost": 1,
            "chain": ["GHOST"],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_root_lists_forecast():
    assert client.get("/").json()["forecast"] == "/forecast"
