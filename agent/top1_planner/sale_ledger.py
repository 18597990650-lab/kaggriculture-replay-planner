"""Cash differences for fixed future sale batches and a deferred current batch."""
import math
from functools import lru_cache


class PriceLadder:
    """Bounded prefix sums of monotone unit prices, at strides one and two."""
    def __init__(self, quote, anchor):
        self.quote_function = quote
        self.anchor = anchor
        self.prices = {}
        self.prefixes = {}

    def quote(self, inventory):
        value = self.prices.get(inventory)
        if value is None:
            if len(self.prices) >= 8192:
                self.prices.clear()
            value = self.quote_function(inventory)
            self.prices[inventory] = value
        return value

    @lru_cache(maxsize=32768)
    def sum_counts(self, inventory, quantity, stride):
        position = math.floor((inventory-self.anchor)/stride)
        origin = inventory-position*stride
        if max(abs(position),abs(position+quantity)) > 8192:
            prices = [self.quote(inventory+index*stride) for index in range(quantity)]
            return sum(prices),sum(price>1 for price in prices)
        key = origin,stride
        series = self.prefixes.get(key)
        if series is None:
            if len(self.prefixes) >= 32:
                self.prefixes.clear()
            series = ([0],[0],[0],[0])
            self.prefixes[key] = series

        def integral(end):
            values, counts = series[:2] if end>=0 else series[2:]
            target = abs(end)
            while len(values)<=target:
                offset = len(values)-1 if end>=0 else -len(values)
                price = self.quote(origin+offset*stride)
                values.append(values[-1]+price)
                counts.append(counts[-1]+(price>1))
            sign = 1 if end>=0 else -1
            return sign*values[target],sign*counts[target]

        low,low_count = integral(position)
        high,high_count = integral(position+quantity)
        return high-low,high_count-low_count


def trade(quote, inventory, own, rival, sum_quotes=None):
    """One shared order slot, with official lockstep quotes and the $1 floor."""
    own_cash = rival_cash = 0
    paired = min(own, rival)
    if sum_quotes is not None:
        if paired:
            own_cash, supplied = sum_quotes(inventory,paired,2)
            rival_cash = own_cash
            inventory += 2*supplied
            if supplied<paired:
                return inventory,own_cash+own-paired,rival_cash+rival-paired
        remaining = abs(own-rival)
        if remaining:
            cash,supplied = sum_quotes(inventory,remaining,1)
            inventory += supplied
            if own>rival:
                own_cash += cash
            else:
                rival_cash += cash
        return inventory,own_cash,rival_cash
    for index in range(paired):
        price = quote(inventory)
        if price == 1:
            own_cash += own - index
            rival_cash += rival - index
            return inventory, own_cash, rival_cash
        own_cash += price
        rival_cash += price
        inventory += 2
    remaining = abs(own - rival)
    cash = 0
    for index in range(remaining):
        price = quote(inventory)
        if price == 1:
            cash += remaining - index
            break
        cash += price
        inventory += 1
    if own > rival:
        own_cash += cash
    else:
        rival_cash += cash
    return inventory, own_cash, rival_cash


def fixed_flow_values(quote, inventory, quantity, max_hold, scenarios, windows, *, sum_quotes=None):
    """Compare until each release window; subsequent policy responses are absent.

    Each event is (tick, own sale units, rival sale units, post-market demand).
    Future batches share one order slot. The deferred current batch uses the
    following slot at its release window, before that tick's consumption.
    """
    window_set = set(windows)
    tables = []
    trade_cache = {}

    def sell(stock, own, rival):
        key = stock, own, rival
        result = trade_cache.get(key)
        if result is None:
            result = trade(quote, stock, own, rival, sum_quotes)
            trade_cache[key] = result
        return result

    initial = [sell(inventory, quantity - hold, 0) for hold in range(max_hold + 1)]
    now_value = initial[0][1]
    tail_values = [now_value - result[1] for result in initial]
    for events in scenarios:
        stocks = [result[0] for result in initial]
        own_cash = [0] * (max_hold + 1)
        rival_cash = [0] * (max_hold + 1)
        rows = {}
        for tick, own, rival, demand in events:
            if own or rival:
                for hold in range(max_hold + 1):
                    stocks[hold], earned, other_earned = sell(stocks[hold], own, rival)
                    own_cash[hold] += earned
                    rival_cash[hold] += other_earned
            if tick in window_set:
                values = [(0, 0, 0)]
                for hold in range(1, max_hold + 1):
                    held_cash = (sum_quotes(stocks[hold], hold, 1)[0] if sum_quotes is not None
                                 else sell(stocks[hold], hold, 0)[1])
                    values.append((held_cash - tail_values[hold],
                                   own_cash[hold] - own_cash[0],
                                   rival_cash[hold] - rival_cash[0]))
                rows[tick] = values
            if demand:
                stocks = [stock - demand for stock in stocks]
        if set(rows) != window_set:
            raise ValueError('Every release window must have a market event')
        tables.append(rows)
    return tables
