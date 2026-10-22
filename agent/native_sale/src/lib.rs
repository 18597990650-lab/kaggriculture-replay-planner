//! Fixed-flow sale comparisons, with the Python policy retaining its task choices.
use std::cell::RefCell;
use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::slice;

mod settlement;
mod demand;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Parameters {
    base: f64,
    anchor: f64,
    threshold: f64,
    below_target: f64,
    above_target: f64,
    below_curve: u32,
    above_curve: u32,
}

#[repr(C)]
#[derive(Clone, Copy, PartialEq)]
pub struct Event {
    tick: i32,
    own: u32,
    rival: u32,
    demand: f64,
}

fn shape(curve: u32, x: f64, threshold: f64) -> f64 {
    let x = x.max(0.0);
    match curve {
        0 => x,
        1 => x * x,
        2 => x.sqrt(),
        3 => (1.0 + x).ln(),
        4 => (1.0 + x).log10(),
        5 => {
            if threshold <= 0.0 {
                x
            } else {
                let u = x / threshold;
                u + 8.0 * (u - 1.0).max(0.0).powf(2.0)
            }
        }
        _ => unreachable!(),
    }
}

fn bits(value: f64) -> u64 {
    // Python's numeric dictionary keys treat both signed zeros as equal.
    if value == 0.0 { 0 } else { value.to_bits() }
}

impl Parameters {
    fn valid(&self) -> bool {
        [self.base, self.anchor, self.threshold, self.below_target, self.above_target]
            .iter().all(|v| v.is_finite() && v.abs() <= 1e9)
            && self.base > 0.0 && self.threshold > 0.0
            && self.below_target >= 0.0 && self.above_target >= 0.0
            && self.below_curve <= 5 && self.above_curve <= 5
    }

    fn key(self) -> [u64; 7] {
        [bits(self.base), bits(self.anchor), bits(self.threshold), bits(self.below_target),
         bits(self.above_target), self.below_curve as u64, self.above_curve as u64]
    }
}

struct Quote {
    parameters: Parameters,
    below_amplitude: f64,
    above_amplitude: f64,
    cache: HashMap<u64, i64>,
}

impl Quote {
    fn new(p: Parameters) -> Self {
        Self {
            parameters: p,
            below_amplitude: p.below_target * p.base / shape(p.below_curve, p.threshold, p.threshold),
            above_amplitude: p.above_target * p.base / shape(p.above_curve, p.threshold, p.threshold),
            cache: HashMap::new(),
        }
    }

    fn price(&mut self, stock: f64) -> i64 {
        let key = bits(stock);
        if let Some(&value) = self.cache.get(&key) { return value; }
        let p = self.parameters;
        let price = if stock < p.anchor {
            p.base + self.below_amplitude * shape(p.below_curve, p.anchor - stock, p.threshold)
        } else {
            p.base - self.above_amplitude * shape(p.above_curve, stock - p.anchor, p.threshold)
        };
        let value = price.round_ties_even().max(1.0) as i64;
        if self.cache.len() >= 8192 { self.cache.clear(); }
        self.cache.insert(key, value);
        value
    }
}

struct Prefix {
    positive_values: Vec<i64>,
    positive_counts: Vec<i64>,
    negative_values: Vec<i64>,
    negative_counts: Vec<i64>,
}

impl Prefix {
    fn new() -> Self {
        Self { positive_values: vec![0], positive_counts: vec![0],
               negative_values: vec![0], negative_counts: vec![0] }
    }

    #[inline(always)]
    fn integral(&mut self, end: i64, origin: f64, stride: u32, quote: &mut Quote) -> (i64, i64) {
        let (values, counts) = if end >= 0 {
            (&mut self.positive_values, &mut self.positive_counts)
        } else {
            (&mut self.negative_values, &mut self.negative_counts)
        };
        let target = end.unsigned_abs() as usize;
        while values.len() <= target {
            let offset = if end >= 0 { values.len() as i64 - 1 } else { -(values.len() as i64) };
            let price = quote.price(origin + (offset * stride as i64) as f64);
            values.push(values.last().unwrap() + price);
            counts.push(counts.last().unwrap() + i64::from(price > 1));
        }
        let sign = if end >= 0 { 1 } else { -1 };
        (sign * values[target], sign * counts[target])
    }
}

struct PriceLadder {
    quote: Quote,
    prefixes: Vec<((u64, u32), Prefix)>,
}

impl PriceLadder {

    fn initial_batch(&mut self, inventory: f64, quantity: u32, max_hold: u32) -> Vec<(f64, i64, i64)> {
        let position = (inventory - self.quote.parameters.anchor).floor() as i64;
        if position.abs().max((position + quantity as i64).abs()) > 8192 {
            return (0..=max_hold).map(|h| self.trade(inventory, quantity - h, 0)).collect();
        }
        let origin = inventory - position as f64;
        let key = (bits(origin), 1);
        let index = if let Some(index) = self.prefixes.iter().position(|(k, _)| *k == key) {
            index
        } else {
            if self.prefixes.len() >= 32 { self.prefixes.clear(); }
            self.prefixes.push((key, Prefix::new()));
            self.prefixes.len()-1
        };
        let series = &mut self.prefixes[index].1;
        let low = series.integral(position, origin, 1, &mut self.quote);
        series.integral(position + quantity as i64, origin, 1, &mut self.quote);
        (0..=max_hold).map(|hold| {
            let end = position + (quantity - hold) as i64;
            let sign = if end >= 0 { 1 } else { -1 };
            let at = end.unsigned_abs() as usize;
            let (values, counts) = if end >= 0 {
                (&series.positive_values, &series.positive_counts)
            } else {
                (&series.negative_values, &series.negative_counts)
            };
            let cash = sign * values[at] - low.0;
            let supplied = sign * counts[at] - low.1;
            (inventory + supplied as f64, cash, 0)
        }).collect()
    }

    fn held_batch(&mut self, stocks: &[f64], output: &mut [i64]) {
        if stocks.len() <= 1 { return; }
        let anchor = self.quote.parameters.anchor;
        let first = (stocks[1] - anchor).floor() as i64;
        let origin = stocks[1] - first as f64;
        let key = bits(origin);
        let mut positions = [0_i64; 101];
        let mut low = first;
        let mut high = first + 1;
        let mut shared = stocks.len() <= positions.len();
        if shared {
            for hold in 1..stocks.len() {
                let position = (stocks[hold] - anchor).floor() as i64;
                if bits(stocks[hold] - position as f64) != key {
                    shared = false;
                    break;
                }
                positions[hold] = position;
                low = low.min(position);
                high = high.max(position + hold as i64);
            }
            shared &= low.abs().max(high.abs()) <= 8192;
        }
        if !shared {
            for hold in 1..stocks.len() {
                output[hold] = self.sum_counts(stocks[hold], hold as u32, 1).0;
            }
            return;
        }
        let series_key = (key, 1);
        let index = if let Some(index) = self.prefixes.iter().position(|(k, _)| *k == series_key) {
            index
        } else {
            if self.prefixes.len() >= 32 { self.prefixes.clear(); }
            self.prefixes.push((series_key, Prefix::new()));
            self.prefixes.len()-1
        };
        let series = &mut self.prefixes[index].1;
        series.integral(low, origin, 1, &mut self.quote);
        series.integral(high, origin, 1, &mut self.quote);
        for hold in 1..stocks.len() {
            let start = positions[hold];
            let end = start + hold as i64;
            let a = if start >= 0 { series.positive_values[start as usize] }
                    else { -series.negative_values[start.unsigned_abs() as usize] };
            let b = if end >= 0 { series.positive_values[end as usize] }
                    else { -series.negative_values[end.unsigned_abs() as usize] };
            output[hold] = b - a;
        }
    }

    fn new(p: Parameters) -> Self {
        Self { quote: Quote::new(p), prefixes: Vec::new() }
    }

    #[inline(always)]
    fn sum_counts(&mut self, inventory: f64, quantity: u32, stride: u32) -> (i64, i64) {
        let position = ((inventory - self.quote.parameters.anchor) / stride as f64).floor() as i64;
        let origin = inventory - (position * stride as i64) as f64;
        let value = if position.abs().max((position + quantity as i64).abs()) > 8192 {
            let mut cash = 0;
            let mut count = 0;
            for index in 0..quantity {
                let price = self.quote.price(inventory + (index * stride) as f64);
                cash += price;
                count += i64::from(price > 1);
            }
            (cash, count)
        } else {
            let series_key = (bits(origin), stride);
            let index = if let Some(index) = self.prefixes.iter().position(|(key, _)| *key == series_key) {
                index
            } else {
                if self.prefixes.len() >= 32 { self.prefixes.clear(); }
                self.prefixes.push((series_key, Prefix::new()));
                self.prefixes.len()-1
            };
            let series = &mut self.prefixes[index].1;
            let low = series.integral(position, origin, stride, &mut self.quote);
            let high = series.integral(position + quantity as i64, origin, stride, &mut self.quote);
            (high.0 - low.0, high.1 - low.1)
        };
        value
    }

    #[inline(always)]
    fn trade(&mut self, mut inventory: f64, own: u32, rival: u32) -> (f64, i64, i64) {
        let paired = own.min(rival);
        let mut own_cash = 0;
        let mut rival_cash = 0;
        if paired > 0 {
            let (cash, supplied) = self.sum_counts(inventory, paired, 2);
            own_cash = cash;
            rival_cash = cash;
            inventory += (2 * supplied) as f64;
            if supplied < paired as i64 {
                return (inventory, own_cash + (own - paired) as i64,
                        rival_cash + (rival - paired) as i64);
            }
        }
        let remaining = own.abs_diff(rival);
        if remaining > 0 {
            let (cash, supplied) = self.sum_counts(inventory, remaining, 1);
            inventory += supplied as f64;
            if own > rival { own_cash += cash; } else { rival_cash += cash; }
        }
        (inventory, own_cash, rival_cash)
    }
}

thread_local! {
    static BOOKS: RefCell<HashMap<[u64; 7], PriceLadder>> = RefCell::new(HashMap::new());
}


enum WindowIndex {
    Dense { origin: i64, slots: [i16; 145] },
    Sparse(HashMap<i32, usize>),
}

impl WindowIndex {
    fn new(windows: &[i32]) -> Result<Self, i32> {
        let origin = windows.iter().copied().min().unwrap_or(0) as i64;
        let end = windows.iter().copied().max().unwrap_or(0) as i64;
        if end - origin < 145 {
            let mut slots = [-1_i16; 145];
            for (index, &tick) in windows.iter().enumerate() {
                let slot = &mut slots[(tick as i64 - origin) as usize];
                if *slot >= 0 { return Err(2); }
                *slot = index as i16;
            }
            Ok(Self::Dense { origin, slots })
        } else {
            let map: HashMap<i32, usize> = windows.iter().enumerate().map(|(i, &t)| (t, i)).collect();
            if map.len() != windows.len() { return Err(2); }
            Ok(Self::Sparse(map))
        }
    }

    #[inline(always)]
    fn get(&self, tick: i32) -> Option<usize> {
        match self {
            Self::Dense { origin, slots } => {
                let offset = tick as i64 - origin;
                if !(0..145).contains(&offset) { return None; }
                let index = slots[offset as usize];
                if index < 0 { None } else { Some(index as usize) }
            }
            Self::Sparse(map) => map.get(&tick).copied(),
        }
    }
}

fn compare(book: &mut PriceLadder, inventory: f64, quantity: u32, max_hold: u32,
           events: &[Event], offsets: &[usize], windows: &[i32]) -> Result<Vec<i64>, i32> {
    let indexes = WindowIndex::new(windows)?;
    compare_indexed(book, inventory, quantity, max_hold, events, offsets, windows, &indexes)
}

fn compare_indexed(book: &mut PriceLadder, inventory: f64, quantity: u32, max_hold: u32,
           events: &[Event], offsets: &[usize], windows: &[i32], indexes: &WindowIndex) -> Result<Vec<i64>, i32> {
    let holds = max_hold as usize + 1;
    let width = windows.len() * holds * 3;
    let mut tables = vec![0; (offsets.len() - 1) * width];
    let initial = book.initial_batch(inventory, quantity, max_hold);
    let mut held_values = vec![0; holds];
    let tail_values: Vec<_> = initial.iter().map(|value| initial[0].1 - value.1).collect();
    for scenario in 0..offsets.len() - 1 {
        let current = &events[offsets[scenario]..offsets[scenario + 1]];
        if let Some(previous) = (0..scenario).find(|&other|
                current == &events[offsets[other]..offsets[other + 1]]) {
            tables.copy_within(previous * width..(previous + 1) * width, scenario * width);
            continue;
        }
        let mut stocks: Vec<_> = initial.iter().map(|value| value.0).collect();
        let mut own_cash = vec![0; holds];
        let mut rival_cash = vec![0; holds];
        let mut seen = vec![false; windows.len()];
        for event in &events[offsets[scenario]..offsets[scenario + 1]] {
            if event.own != 0 || event.rival != 0 {
                for hold in 0..holds {
                    let (stock, cash, other_cash) = book.trade(stocks[hold], event.own, event.rival);
                    stocks[hold] = stock;
                    own_cash[hold] += cash;
                    rival_cash[hold] += other_cash;
                }
            }
            if let Some(index) = indexes.get(event.tick) {
                seen[index] = true;
                let base = scenario * width + index * holds * 3;
                book.held_batch(&stocks, &mut held_values);
                for hold in 1..holds {
                    let held_cash = held_values[hold];
                    tables[base + hold * 3] = held_cash - tail_values[hold];
                    tables[base + hold * 3 + 1] = own_cash[hold] - own_cash[0];
                    tables[base + hold * 3 + 2] = rival_cash[hold] - rival_cash[0];
                }
            }
            if event.demand != 0.0 {
                for stock in &mut stocks { *stock -= event.demand; }
            }
        }
        if seen.iter().any(|value| !value) { return Err(3); }
    }
    Ok(tables)
}

fn reduce(tables: &[i64], windows: &[i32], holds: usize, start: i32, output: &mut [i64]) {
    let width = windows.len() * holds * 3;
    output.fill(0);
    for row in output.chunks_exact_mut(6) { row[1] = start as i64; }
    for (index, &tick) in windows.iter().enumerate() {
        for hold in 1..holds {
            let offset = index * holds * 3 + hold * 3;
            let a = &tables[offset..offset + 3];
            let b = &tables[width + offset..width + offset + 3];
            let a_score = a[0] + a[1] - a[2];
            let b_score = b[0] + b[1] - b[2];
            let (score, chosen) = if b_score < a_score { (b_score, b) } else { (a_score, a) };
            let row = &mut output[hold * 6..hold * 6 + 6];
            if score > row[0] {
                row.copy_from_slice(&[score, tick as i64, chosen[0] + chosen[1], chosen[2], chosen[0], chosen[1]]);
            }
        }
    }
}

#[no_mangle]
pub extern "C" fn fv_abi_version() -> u32 { 1 }

/// Pointers are caller-owned arrays of the supplied lengths; no pointers escape.
#[no_mangle]
pub unsafe extern "C" fn fv_evaluate(
    parameters: *const Parameters, inventory: f64, quantity: u32, max_hold: u32, start: i32,
    events: *const Event, event_count: usize, offsets: *const usize, scenario_count: usize,
    windows: *const i32, window_count: usize,
    tables: *mut i64, table_count: usize, offers: *mut i64, offer_count: usize,
) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), i32> {
        if parameters.is_null() || offsets.is_null() || events.is_null() || windows.is_null() {
            return Err(2);
        }
        let p = *parameters;
        if !p.valid() { return Err(1); }
        if !inventory.is_finite() || inventory.abs() > 1e9 || quantity > 10000 || max_hold > quantity
            || max_hold > 100 || scenario_count == 0 || scenario_count > 8 || event_count > 8192
            || window_count > 720 { return Err(2); }
        let events = slice::from_raw_parts(events, event_count);
        let offsets = slice::from_raw_parts(offsets, scenario_count + 1);
        let windows = slice::from_raw_parts(windows, window_count);
        if offsets[0] != 0 || offsets[scenario_count] != event_count
            || offsets.windows(2).any(|w| w[0] > w[1]) { return Err(2); }
        if events.iter().any(|e| !e.demand.is_finite() || e.demand.abs() > 1e6
            || e.own > 10000 || e.rival > 10000) { return Err(2); }
        let holds = max_hold as usize + 1;
        let required_tables = scenario_count * window_count * holds * 3;
        if table_count != 0 && (tables.is_null() || table_count != required_tables) { return Err(2); }
        if offer_count != 0 && (offers.is_null() || offer_count != holds * 6 || scenario_count != 2) {
            return Err(2);
        }
        let values = BOOKS.with(|books| {
            let mut books = books.borrow_mut();
            let key = p.key();
            if !books.contains_key(&key) && books.len() >= 36 { books.clear(); }
            let book = books.entry(key).or_insert_with(|| PriceLadder::new(p));
            compare(book, inventory, quantity, max_hold, events, offsets, windows)
        })?;
        if table_count != 0 {
            slice::from_raw_parts_mut(tables, table_count).copy_from_slice(&values);
        }
        if offer_count != 0 {
            reduce(&values, windows, holds, start, slice::from_raw_parts_mut(offers, offer_count));
        }
        Ok(())
    }));
    match result { Ok(Ok(())) => 0, Ok(Err(code)) => code, Err(_) => 5 }
}

#[no_mangle]
pub extern "C" fn fv_clear_cache() {
    BOOKS.with(|books| books.borrow_mut().clear());
}

/// Prepare exact price prefixes; no farm state, future shop or action is assumed.
#[no_mangle]
pub unsafe extern "C" fn fv_prepare_prices(parameters: *const Parameters, inventory: f64,
                                           radius: u32, phase: u32) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), i32> {
        if parameters.is_null() || !inventory.is_finite() || inventory.abs() > 1e9 || radius > 1024 || phase > 15 { return Err(2); }
        let p = *parameters;
        if !p.valid() { return Err(1); }
        BOOKS.with(|books| {
            let mut books = books.borrow_mut();
            if !books.contains_key(&p.key()) && books.len() >= 36 { books.clear(); }
            let book = books.entry(p.key()).or_insert_with(|| PriceLadder::new(p));
            let origin = inventory.floor()+phase as f64/8.0;
            book.sum_counts(origin-radius as f64, radius*2, 1);
            book.sum_counts(origin-radius as f64, radius, 2);
        });
        Ok(())
    }));
    match result { Ok(Ok(())) => 0, Ok(Err(code)) => code, Err(_) => 5 }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn milk() -> Parameters {
        Parameters { base: 160.0, anchor: 10000.0, threshold: 122.0,
                     below_target: 0.6, above_target: 1.6, below_curve: 2, above_curve: 0 }
    }

    #[test]
    fn floor_stops_supply_but_pays_every_unit() {
        let mut book = PriceLadder::new(milk());
        assert_eq!(book.trade(11000.0, 20, 30), (11000.0, 20, 30));
        assert_eq!(book.trade(11000.0, 0, 30), (11000.0, 0, 30));
    }

    #[test]
    fn paired_units_share_the_quote() {
        let mut book = PriceLadder::new(milk());
        assert_eq!(book.trade(10000.0, 1, 1), (10002.0, 160, 160));
    }

    #[test]
    fn release_precedes_consumption() {
        let mut book = PriceLadder::new(milk());
        let events = [Event { tick: 5, own: 0, rival: 0, demand: 100.0 }];
        let table = compare(&mut book, 10000.0, 1, 1, &events, &[0, 1], &[5]).unwrap();
        assert_eq!(table, [0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn equal_scores_retain_the_first_world_and_window() {
        let mut result = [0; 12];
        let values = [0, 0, 0, 2, 1, 0, 0, 0, 0, 4, 0, 1,
                      0, 0, 0, 3, 2, 2, 0, 0, 0, 5, 1, 3];
        reduce(&values, &[5, 9], 2, 1, &mut result);
        assert_eq!(&result[6..], [3, 5, 3, 0, 2, 1]);
    }
}
