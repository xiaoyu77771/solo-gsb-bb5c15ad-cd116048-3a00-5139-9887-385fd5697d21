"""等待时间预测：不扣减状态，计算链上全部桶首次同时满足成本的时刻。"""
from __future__ import annotations

from app.models import EPS, BucketForecast, ForecastRequest, ForecastResponse
from app.simulation import _round


def forecast(payload: ForecastRequest) -> ForecastResponse:
    specs = {b.id: b for b in payload.buckets}
    cost = float(payload.cost)

    def current_balance(bucket_id: str) -> float:
        spec = specs[bucket_id]
        return float(spec.capacity if spec.balance is None else spec.balance)

    views: dict[str, BucketForecast] = {}
    waits: dict[str, float] = {}
    unreachable: list[str] = []

    for bucket_id in payload.chain:
        spec = specs[bucket_id]
        capacity = float(spec.capacity)
        rate = float(spec.refill_rate)
        balance = current_balance(bucket_id)

        deficit = cost - balance
        if deficit <= EPS:
            # 当前余额已满足成本
            reachable = True
            wait: float | None = 0.0
        elif cost > capacity + EPS or rate <= 0:
            # 容量永远装不下成本，或零速率且余额不足：不可达
            reachable = False
            wait = None
        else:
            # 连续补充 deficit / rate 秒后恰好达到成本（cost <= capacity，不会触顶）
            reachable = True
            wait = deficit / rate

        if reachable:
            waits[bucket_id] = wait
        else:
            unreachable.append(bucket_id)

        views[bucket_id] = BucketForecast(
            balance_now=_round(balance),
            reachable=reachable,
            wait_seconds=None if wait is None else _round(wait),
            projected_balance=None,  # 整体可达时在下方统一回填
        )

    if unreachable:
        return ForecastResponse(
            now=_round(payload.now),
            cost=_round(cost),
            reachable=False,
            wait_seconds=None,
            available_at=None,
            limited_by=None,
            unreachable_buckets=unreachable,
            buckets=views,
        )

    # 整体等待 = 各桶等待的最大值；限制桶为链上第一个达到最长等待的桶
    wait_seconds = max(waits.values())
    available_at = payload.now + wait_seconds
    limited_by = next(
        bucket_id for bucket_id in payload.chain if waits[bucket_id] >= wait_seconds - EPS
    )

    for bucket_id in payload.chain:
        spec = specs[bucket_id]
        projected = min(
            float(spec.capacity),
            current_balance(bucket_id) + float(spec.refill_rate) * wait_seconds,
        )
        views[bucket_id].projected_balance = _round(projected)

    return ForecastResponse(
        now=_round(payload.now),
        cost=_round(cost),
        reachable=True,
        wait_seconds=_round(wait_seconds),
        available_at=_round(available_at),
        limited_by=limited_by,
        unreachable_buckets=[],
        buckets=views,
    )
