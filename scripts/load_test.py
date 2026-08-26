#!/usr/bin/env python3
"""Concurrent checkout traffic generator for checkout-api.

Drives the real HTTP API so it produces genuine traffic, latency metrics and
failure logs (useful for exercising the OpsPilot alert path).

Examples:
    python scripts/load_test.py --users 25 --requests 5
    python scripts/load_test.py --users 150 --requests 20 --payment-method unstable_gateway
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


@dataclass
class Stats:
    statuses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)

    def record(self, status: int, latency: float, body: str = "") -> None:
        self.statuses[status] += 1
        self.latencies.append(latency)
        if status >= 400 or status == 0:
            self.errors[body[:120] or f"HTTP {status}"] += 1

    def report(self) -> None:
        total = sum(self.statuses.values())
        if not total:
            print("no requests were made")
            return
        ok = sum(count for code, count in self.statuses.items() if 0 < code < 400)
        print("\n=== load test summary ===")
        print(f"requests:   {total}")
        print(f"succeeded:  {ok} ({ok / total:.1%})")
        for code, count in sorted(self.statuses.items()):
            print(f"  HTTP {code}: {count}")
        ordered = sorted(self.latencies)
        p = lambda q: ordered[min(int(len(ordered) * q), len(ordered) - 1)] * 1000  # noqa: E731
        print(f"latency ms: p50={p(0.5):.0f} p95={p(0.95):.0f} p99={p(0.99):.0f} max={p(1.0):.0f}")
        if self.errors:
            print("top errors:")
            for body, count in self.errors.most_common(5):
                print(f"  {count:>4}x {body}")


async def prepare(client: httpx.AsyncClient, customers: int) -> tuple[list[str], list[str]]:
    """Create (or reuse) test customers and read the product catalog."""
    customer_ids: list[str] = []
    for i in range(customers):
        response = await client.post(
            "/api/v1/customers",
            json={"email": f"loadgen+{i}@example.com", "first_name": "Load", "last_name": f"Gen{i}"},
        )
        if response.status_code in (200, 201):
            customer_ids.append(response.json()["id"])

    catalog = await client.get("/api/v1/products", params={"limit": 50})
    product_ids = [p["id"] for p in catalog.json()] if catalog.status_code == 200 else []
    return customer_ids, product_ids


async def shopper(
    client: httpx.AsyncClient,
    *,
    user_id: int,
    requests_per_user: int,
    customer_ids: list[str],
    product_ids: list[str],
    payment_methods: list[str],
    stats: Stats,
    think_time: tuple[float, float],
) -> None:
    """One virtual user: build a cart then check out, repeated N times."""
    for run in range(requests_per_user):
        started = time.perf_counter()
        customer_id = random.choice(customer_ids) if customer_ids else None
        headers = {
            "X-Request-ID": f"load-{user_id}-{run}",
            "Idempotency-Key": f"load-{user_id}-{run}-{random.randint(0, 1_000_000)}",
        }
        if customer_id:
            headers["X-Customer-ID"] = customer_id

        try:
            cart_resp = await client.post(
                "/api/v1/carts",
                json={"customer_id": customer_id} if customer_id else {"session_id": f"load-{user_id}"},
                headers=headers,
            )
            if cart_resp.status_code not in (200, 201):
                stats.record(cart_resp.status_code, time.perf_counter() - started, cart_resp.text)
                continue

            cart_id = cart_resp.json()["id"]
            for _ in range(random.randint(1, 4)):
                product_id = random.choice(product_ids)
                item_resp = await client.post(
                    f"/api/v1/carts/{cart_id}/items",
                    json={"product_id": product_id, "quantity": random.randint(1, 3)},
                    headers=headers,
                )
                if item_resp.status_code >= 400:
                    stats.record(item_resp.status_code, time.perf_counter() - started, item_resp.text)
                    break

            checkout_resp = await client.post(
                "/api/v1/orders/checkout",
                json={"cart_id": cart_id, "payment_method": random.choice(payment_methods)},
                headers=headers,
            )
            stats.record(checkout_resp.status_code, time.perf_counter() - started, checkout_resp.text)
        except Exception as exc:
            stats.record(0, time.perf_counter() - started, f"{type(exc).__name__}: {exc}")

        await asyncio.sleep(random.uniform(*think_time))


async def main() -> None:
    parser = argparse.ArgumentParser(description="checkout-api load generator")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=25, help="concurrent virtual users")
    parser.add_argument("--requests", type=int, default=5, help="checkouts per user")
    parser.add_argument("--customers", type=int, default=30, help="distinct customers to create")
    parser.add_argument("--ramp-up", type=float, default=5.0, help="seconds to spread user starts over")
    parser.add_argument("--think-min", type=float, default=0.05)
    parser.add_argument("--think-max", type=float, default=0.3)
    parser.add_argument(
        "--payment-method",
        action="append",
        dest="payment_methods",
        help="repeatable; defaults to credit_card/paypal/bank_transfer",
    )
    args = parser.parse_args()

    payment_methods = args.payment_methods or ["credit_card", "paypal", "bank_transfer"]
    stats = Stats()

    async with httpx.AsyncClient(
        base_url=args.url,
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    ) as client:
        customer_ids, product_ids = await prepare(client, args.customers)
        print(f"prepared {len(customer_ids)} customers and {len(product_ids)} products")
        if not product_ids:
            print("no products found - run scripts/init-db.sql or POST /api/v1/products first")
            return

        started = time.perf_counter()
        tasks = [
            asyncio.create_task(
                shopper(
                    client,
                    user_id=user_id,
                    requests_per_user=args.requests,
                    customer_ids=customer_ids,
                    product_ids=product_ids,
                    payment_methods=payment_methods,
                    stats=stats,
                    think_time=(args.think_min, args.think_max),
                )
            )
            for user_id in range(args.users)
        ]
        for task in tasks:
            await asyncio.sleep(args.ramp_up / len(tasks) if args.ramp_up else 0)
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    stats.report()
    total = sum(stats.statuses.values())
    if total:
        print(f"wall clock: {elapsed:.1f}s ({total / elapsed:.1f} req/s)")


if __name__ == "__main__":
    asyncio.run(main())
