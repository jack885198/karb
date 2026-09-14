"""Read-only Polymarket paper research. Standard library only; no signing or orders.

Run: PYTHONPATH=src python -m karb.paper_ev --seconds 60 --markets 10
Paper fills use refreshed displayed depth, not queue/settlement simulation.
Fees are conservative cash-equivalent costs; token-denominated fee collection
and actual merge execution are not simulated. Output never certifies positive EV.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

D = Decimal
ZERO = D("0")
ONE = D("1")


def decimal(value):
    x = D(str(value))
    if not x.is_finite():
        raise ValueError("non_finite")
    return x


def fee_rate(market):
    if market.get("feesEnabled") is False:
        return ZERO
    schedule = market.get("feeSchedule") or {}
    if (market.get("feesEnabled") is not True
            or schedule.get("exponent") != 1 or schedule.get("takerOnly") is not True
            or "rate" not in schedule):
        raise ValueError("unknown_fee_schedule")
    rate = decimal(schedule["rate"])
    if not ZERO <= rate <= ONE:
        raise ValueError("invalid_fee")
    return rate


def market_tokens(market):
    if (market.get("active") is not True or market.get("closed") is not False
            or market.get("acceptingOrders") is not True
            or market.get("enableOrderBook") is not True):
        raise ValueError("market_not_tradeable")
    # Restrict this first experiment to standard binary conditions.
    if market.get("negRisk") is not False:
        raise ValueError("nonstandard_condition")
    if not market.get("conditionId"):
        raise ValueError("missing_condition")
    outcomes = market.get("outcomes")
    tokens = market.get("clobTokenIds")
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
    if (not isinstance(outcomes, list) or not isinstance(tokens, list)
            or len(outcomes) != 2 or len(tokens) != 2
            or set(outcomes) != {"Yes", "No"} or len(set(tokens)) != 2):
        raise ValueError("invalid_pair")
    return dict(zip(outcomes, tokens))


def validate_books(market, books, now_ms, max_age_ms=2000, max_skew_ms=1000):
    tokens = market_tokens(market)
    stamps = []
    for outcome in ("Yes", "No"):
        book = books[outcome]
        if (str(book.get("asset_id")) != str(tokens[outcome])
                or str(book.get("market")).lower() != market["conditionId"].lower()):
            raise ValueError("book_identity_mismatch")
        stamp = int(book["timestamp"])
        age = now_ms - stamp
        if age < -500 or age > max_age_ms:
            raise ValueError("stale_book")
        stamps.append(stamp)
    if abs(stamps[0] - stamps[1]) > max_skew_ms:
        raise ValueError("unsynchronized_books")


def walk(book, side, quantity, rate, limit=None):
    """Cost/proceeds, conservatively rounded fee, marginal price; full depth required."""
    if quantity <= ZERO:
        raise ValueError("invalid_quantity")
    levels = []
    for level in book.get(side, []):
        price, size = decimal(level["price"]), decimal(level["size"])
        if not ZERO < price < ONE or size < ZERO:
            raise ValueError("invalid_level")
        if size:
            levels.append((price, size))
    levels.sort(reverse=(side == "bids"))
    remaining, amount, fee, marginal = quantity, ZERO, ZERO, ZERO
    for price, size in levels:
        if limit is not None and ((side == "asks" and price > limit)
                                  or (side == "bids" and price < limit)):
            continue
        fill = min(remaining, size)
        amount += fill * price
        fee += (fill * rate * price * (ONE-price)).quantize(
            D("0.00001"), rounding=ROUND_CEILING)
        marginal = price
        remaining -= fill
        if remaining == ZERO:
            return amount, fee, marginal
    raise ValueError("insufficient_depth")


def quote(market, books, quantity, now_ms, cash, overhead=D("0.05")):
    validate_books(market, books, now_ms)
    rate = fee_rate(market)
    minimum = decimal(market.get("orderMinSize", 5))
    if quantity < minimum or overhead < ZERO or cash < ZERO:
        raise ValueError("invalid_size_or_budget")
    legs = {s: walk(books[s], "asks", quantity, rate) for s in ("Yes", "No")}
    if any(v[0] < ONE for v in legs.values()):
        raise ValueError("order_value_below_model_minimum")
    buy_cost = sum(v[0] for v in legs.values())
    fees = sum(v[1] for v in legs.values())
    cost = buy_cost + fees + overhead
    if cost > cash:
        raise ValueError("insufficient_cash")
    return {"shares": quantity, "buy_cost": buy_cost, "fees": fees,
            "overhead": overhead, "cost": cost, "net_if_merged": quantity-cost,
            "limits": {s: v[2] for s, v in legs.items()}}


def delayed_paper_fill(market, books, signal, now_ms):
    """Independent FOK-like depth checks; a single filled leg is NOT a winning set."""
    validate_books(market, books, now_ms)
    rate, quantity = fee_rate(market), signal["shares"]
    fills = {}
    for side in ("Yes", "No"):
        try:
            fills[side] = walk(books[side], "asks", quantity, rate, signal["limits"][side])
        except ValueError as exc:
            if str(exc) != "insufficient_depth":
                raise
    if len(fills) == 2:
        cost = sum(v[0]+v[1] for v in fills.values()) + signal["overhead"]
        return {"state": "paired_paper", "debit": cost, "credit": ZERO,
                "pending_model_credit": quantity, "net_if_merged": quantity-cost}
    if not fills:
        return {"state": "missed", "debit": ZERO, "credit": ZERO}
    side, fill = next(iter(fills.items()))
    cost = fill[0]+fill[1]+signal["overhead"]
    try:
        proceeds, fees, _ = walk(books[side], "bids", quantity, rate)
        return {"state": "single_leg_unwind_paper", "debit": cost,
                "credit": proceeds-fees, "paper_pnl": proceeds-fees-cost}
    except ValueError as exc:
        if str(exc) != "insufficient_depth":
            raise
        return {"state": "unresolved_single_leg", "debit": cost, "credit": ZERO,
                "outcome": side, "shares": quantity, "paper_pnl": None}


def get_json(base, params):
    req = Request(base+"?"+urlencode(params), headers={"User-Agent": "karb-paper-research/1"})
    # GET only. No credentials, proxy routing, signing, or trading requests.
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def books_for(market):
    tokens = market_tokens(market)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {s: pool.submit(get_json, "https://clob.polymarket.com/book",
                               {"token_id": t}) for s, t in tokens.items()}
        return {s: f.result() for s, f in jobs.items()}


def run(args):
    if not (1 <= args.seconds <= 3600 and 1 <= args.markets <= 50
            and 0 <= args.latency_ms <= 5000 and args.interval >= 1
            and args.shares > 0 and args.cash > 0 and args.min_net >= 0):
        raise ValueError("invalid_arguments")
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out/"observations.jsonl"
    summary = {"status": "running", "source": "public_api", "certified_positive_ev": False,
               "markets": 0, "observations": 0, "candidates": 0, "paired_paper": 0,
               "errors": 0, "rejections": {}, "actual_orders": 0, "actual_realized_pnl": 0,
               "limitations": ["displayed depth is not a fill guarantee",
                 "cash-equivalent fees, not actual net-token settlement",
                 "single-leg unwind has no additional network delay",
                 "no calibrated fill probabilities or statistical EV",
                 "standard binary only; no negative-risk conversion"]}
    cash, used, pending = decimal(args.cash), set(), []
    start = time.monotonic()

    def emit(event):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"observed_at_ms": int(time.time()*1000), **event},
                               ensure_ascii=False, default=str)+"\n")

    try:
        raw = get_json("https://gamma-api.polymarket.com/markets",
                       {"active": "true", "closed": "false", "limit": 100})
        if not isinstance(raw, list):
            raise ValueError("invalid_market_response")
        markets = []
        for m in raw:
            try:
                market_tokens(m)
                fee_rate(m)
                markets.append(m)
            except (ValueError, TypeError, KeyError) as exc:
                key = str(exc)
                summary["rejections"][key] = summary["rejections"].get(key, 0)+1
            if len(markets) >= args.markets:
                break
        summary["markets"] = len(markets)
        while markets and time.monotonic()-start < args.seconds:
            for m in markets:
                if time.monotonic()-start >= args.seconds:
                    break
                identity = m["conditionId"]
                if identity in used:
                    continue
                try:
                    # Refresh fee/market metadata on every evaluation.
                    rows = get_json("https://gamma-api.polymarket.com/markets",
                                    {"id": m["id"]})
                    if not rows or rows[0].get("conditionId") != identity:
                        raise ValueError("market_changed")
                    m = rows[0]
                    books = books_for(m)
                    result = quote(m, books, decimal(args.shares),
                                   int(time.time()*1000), cash)
                    summary["observations"] += 1
                    emit({"kind": "quote", "market": identity, "metadata": m,
                          "books": books, "result": result})
                    if result["net_if_merged"] < decimal(args.min_net):
                        continue
                    summary["candidates"] += 1
                    time.sleep(args.latency_ms/1000)
                    after = books_for(m)
                    fill = delayed_paper_fill(m, after, result, int(time.time()*1000))
                    if fill["debit"] > cash:
                        raise ValueError("execution_exceeds_cash")
                    cash += fill["credit"] - fill["debit"]
                    # Reserve paired collateral for the entire run. Do not invent instant
                    # merge confirmations or repeatedly consume the same market liquidity.
                    used.add(identity)
                    if fill["state"] == "paired_paper":
                        summary["paired_paper"] += 1
                        pending.append({"market": identity, **fill})
                    elif fill["state"] == "unresolved_single_leg":
                        pending.append({"market": identity, **fill})
                    emit({"kind": "paper_execution", "market": identity,
                          "books_after_latency": after, **fill, "cash_remaining": cash})
                except (ValueError, TypeError, KeyError) as exc:
                    reason = str(exc)
                    summary["rejections"][reason] = summary["rejections"].get(reason, 0)+1
                    emit({"kind": "rejection", "market": identity, "reason": reason})
                except Exception as exc:
                    summary["errors"] += 1
                    emit({"kind": "network_error", "market": identity,
                          "error": type(exc).__name__+": "+str(exc)})
            if time.monotonic()-start < args.seconds:
                time.sleep(min(args.interval, args.seconds-(time.monotonic()-start)))
        summary["status"] = ("completed_observation" if summary["observations"]
                             else "blocked_or_no_usable_data")
    except Exception as exc:
        summary["status"] = "blocked"
        summary["errors"] += 1
        summary["error"] = type(exc).__name__+": "+str(exc)
    finally:
        summary["cash_remaining"] = cash
        summary["pending_positions"] = pending
        summary["elapsed_seconds"] = round(time.monotonic()-start, 2)
        summary["sample_scope"] = "first 100 API markets, bounded eligible subset; not whole market"
        (out/"summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                       encoding="utf-8")
        print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["observations"] else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--markets", type=int, default=10)
    p.add_argument("--shares", type=float, default=20)
    p.add_argument("--cash", type=float, default=1000)
    p.add_argument("--latency-ms", type=int, default=500)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--min-net", type=float, default=0.10)
    p.add_argument("--output", default="paper-results")
    raise SystemExit(run(p.parse_args()))


if __name__ == "__main__":
    main()
