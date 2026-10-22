//! Complete bounded sale allocation. All state is supplied by the current caller.
use super::*;

const N: usize = 9;

#[repr(C)]
struct State {
    start: i32,
    end: i32,
    capacity: u32,
    cargo_origin: i32,
    cargo_flags: u32,
    asset_count: u32,
    money: f64,
    cash_floor: f64,
    inventory: [f64; N],
    consumption: [f64; N],
    amounts: [u32; N],
    bags: [u32; N],
    cargo: [u32; N],
    upper: [u32; N],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Asset {
    product: i32,
    age: i32,
    held: i32,
    bonus: i32,
    fertilized: i32,
    watered: i32,
    distance: i32,
    side: i32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct SaleRecord {
    item: u32,
    quantity: u32,
    gain: i64,
    own: i64,
    batch: i64,
    other: i64,
    rival: i64,
    window: i64,
}

#[repr(C)]
pub struct SaleResult {
    amounts: [u32; N],
    count: u32,
    cash: f64,
    records: [SaleRecord; N],
}

fn arrivals(state: &State, assets: &[Asset]) -> (Vec<[[u32; N]; 3]>, Vec<i32>) {
    let length = (state.end-state.start+1) as usize;
    let mut flow = vec![[[0; N]; 3]; length];
    let mut imminent = [None, None];
    let mut add = |tick: i32, item: usize, qty: u32, side: usize| {
        if tick < state.start || tick > state.end { return; }
        flow[(tick-state.start) as usize][side][item] += qty;
        if side > 0 && state.start+1 < tick && tick <= state.start+24 {
            let index = side-1;
            imminent[index] = Some(imminent[index].map_or(tick, |old: i32| old.min(tick)));
        }
    };
    let day = state.start/24;
    let days = state.end/24-day+1;
    // Item metadata has the same product order as the rule-owned ABI.
    let first = [2, 2, 8, 10, 10, 4, 8, 6, 0];
    let maximum_day = [4, 3, 8, 10, 12, 0, 0, 0, 0];
    let limit = [6, 4, 4, 4, 6, 4, 6, 6, 0];
    let interval = [0, 0, 1, 2, 0, 1, 2, 3, 0];
    for &asset in assets {
        let p = asset.product as usize;
        let animal = p >= 5;
        let ongoing = p == 2 || p == 3;
        let mut held = asset.held;
        let mut bonus = asset.bonus;
        let mut watered = asset.watered != 0;
        for offset in 0..days {
            if (animal || asset.age+offset >= first[p]) && held > 0 {
                let earliest = state.start.max((day+offset)*24)+asset.distance+2;
                if asset.side == 0 {
                    add(earliest+4, p, held as u32, 0);
                } else {
                    add(earliest+4, p, held as u32, 1);
                    add(earliest+12, p, held as u32, 2);
                }
                held = 0;
                if !animal && !ongoing { break; }
            }
            if animal {
                let elapsed = offset+1+asset.age-first[p];
                if elapsed >= 0 && elapsed % interval[p] == 0 {
                    held = limit[p].min(held+1+bonus);
                    bonus = 0;
                }
                bonus += 1;
            } else {
                if !watered && !ongoing && (maximum_day[p]+1)/2 <= offset+asset.age
                        && offset+asset.age <= maximum_day[p] {
                    held = limit[p].min(held+if asset.fertilized >= offset { 2 } else { 1 });
                }
                if ongoing {
                    let elapsed = offset+1+asset.age-first[p];
                    if elapsed >= 0 && elapsed % interval[p] == 0 && elapsed/interval[p]+1 <= limit[p] {
                        held = limit[p].min(held+if asset.fertilized >= offset { 2 } else { 1 });
                    }
                }
                watered = false;
            }
        }
    }
    let bag_tick = state.end.min((day+1)*24)+4;
    for p in 1..8 { add(bag_tick, p, state.bags[p], 0); }
    for (index, delay) in [4, 12].iter().enumerate() {
        if state.cargo_flags & (1 << index) != 0 {
            let tick = state.cargo_origin+delay;
            for p in 0..N {
                add(tick, p, state.cargo[p]+if index == 1 {state.upper[p]} else {0}, index+1);
            }
        }
    }
    let first_window = state.start+1+(-state.start).rem_euclid(4);
    let mut windows: Vec<_> = (first_window..=state.end).step_by(4).collect();
    windows.push(state.end);
    for tick in imminent.into_iter().flatten() { windows.push(tick-1); }
    windows.sort_unstable();
    windows.dedup();
    (flow, windows)
}


struct Prediction {
    start: i32,
    stop: i32,
    flow: Vec<[u32; N]>,
}

impl Prediction {
    unsafe fn decode(payload: *const u8, length: usize, state: &State) -> Result<Self, i32> {
        if payload.is_null() || length < 8 { return Err(2); }
        let start = std::ptr::read_unaligned(payload.cast::<i32>());
        let stop = std::ptr::read_unaligned(payload.add(4).cast::<i32>());
        if start != state.start || stop < start || stop > state.end || stop-start >= 24 {
            return Err(2);
        }
        let count = (stop-start+1) as usize;
        if length != 8+count*N*4 { return Err(2); }
        let mut flow = Vec::with_capacity(count);
        for index in 0..count {
            let row = std::ptr::read_unaligned(payload.add(8+index*N*4).cast::<[u32; N]>());
            if row.iter().any(|q| *q > 10000) || row[0] != 0 || row[8] != 0 { return Err(2); }
            flow.push(row);
        }
        Ok(Self { start, stop, flow })
    }

    fn overlay(&self, state: &State, flow: &mut [[[u32; N]; 3]], windows: &mut Vec<i32>) {
        for (index, quantities) in self.flow.iter().enumerate() {
            for side in 1..=2 {
                flow[index][side][1..8].copy_from_slice(&quantities[1..8]);
            }
        }
        for side in 1..=2 {
            for (index, quantities) in flow.iter().take(self.flow.len()).enumerate() {
                let tick = self.start+index as i32;
                if state.start+1 < tick && tick <= (state.start+24).min(self.stop)
                        && quantities[side].iter().any(|q| *q != 0) {
                    windows.push(tick-1);
                    break;
                }
            }
        }
        windows.sort_unstable();
        windows.dedup();
    }
}

fn run(parameters: &[Parameters], state: &State, assets: &[Asset], prediction: Option<&Prediction>) -> Result<SaleResult, i32> {
    let mut result = SaleResult { amounts: state.amounts, count: 0, cash: state.money,
                                 records: [SaleRecord::default(); N] };
    BOOKS.with(|books| -> Result<(), i32> {
        let mut books = books.borrow_mut();
        if parameters.iter().any(|p| !books.contains_key(&p.key())) && books.len()+N > 36 { books.clear(); }
        let mut prices: [Vec<i64>; N] = std::array::from_fn(|_| Vec::new());
        for p in 0..N {
            let book = books.entry(parameters[p].key()).or_insert_with(|| PriceLadder::new(parameters[p]));
            let mut stock = state.inventory[p];
            for _ in 0..state.amounts[p] {
                let price = book.quote.price(stock);
                prices[p].push(price);
                result.cash += price as f64;
                stock += f64::from(price > 1);
            }
        }
        if result.cash <= state.cash_floor { return Ok(()); }
        let (mut flow, mut windows) = arrivals(state, assets);
        if let Some(prediction) = prediction { prediction.overlay(state, &mut flow, &mut windows); }
        let indexes = WindowIndex::new(&windows)?;
        let mut gains: [Vec<i64>; N] = std::array::from_fn(|_| Vec::new());
        let mut offers = Vec::new();
        // Python heap tuple tie order is the lexical product name order.
        let ranks = [8, 0, 6, 5, 3, 1, 4, 7, 2];
        for p in 1..8 {
            let qty = state.amounts[p];
            if qty == 0 { continue; }
            let max_hold = qty.min(state.capacity);
            let mut events = Vec::new();
            let mut offsets = vec![0];
            for side in 1..=2 {
                for (index, amounts) in flow.iter().enumerate() {
                    let tick = state.start+index as i32;
                    let demand = if tick % 4 == 0 {
                        state.consumption[p]+if tick % 24 == 0 {1.0} else {0.0}
                    } else {0.0};
                    if amounts[0][p] != 0 || amounts[side][p] != 0 || demand != 0.0 || indexes.get(tick).is_some() {
                        events.push(Event {tick, own: amounts[0][p], rival: amounts[side][p], demand});
                    }
                }
                offsets.push(events.len());
            }
            let book = books.get_mut(&parameters[p].key()).unwrap();
            let values = compare_indexed(book, state.inventory[p], qty, max_hold, &events, &offsets, &windows, &indexes)?;
            gains[p].resize((max_hold as usize+1)*6, 0);
            reduce(&values, &windows, max_hold as usize+1, state.start, &mut gains[p]);
            if gains[p][6] > 0 { offers.push((gains[p][6], p, 1usize)); }
        }
        let mut held = [0; N];
        let mut order = Vec::new();
        let mut allocated = 0;
        while !offers.is_empty() && allocated < state.capacity {
            let index = (0..offers.len()).max_by_key(|&i| (offers[i].0, -ranks[offers[i].1])).unwrap();
            let (_, p, count) = offers.swap_remove(index);
            let cost = prices[p][prices[p].len()-count] as f64;
            if result.cash-cost < state.cash_floor { continue; }
            result.cash -= cost;
            if held[p] == 0 { order.push(p); }
            held[p] = count;
            allocated += 1;
            result.amounts[p] -= 1;
            if (count+1)*6 < gains[p].len() {
                let increment = gains[p][(count+1)*6]-gains[p][count*6];
                if increment > 0 { offers.push((increment, p, count+1)); }
            }
        }
        for p in order {
            let row = &gains[p][held[p]*6..held[p]*6+6];
            result.records[result.count as usize] = SaleRecord {item: p as u32, quantity: held[p] as u32,
                gain: row[0], window: row[1], own: row[2], rival: row[3], batch: row[4], other: row[5]};
            result.count += 1;
        }
        Ok(())
    })?;
    Ok(result)
}

#[no_mangle]
pub extern "C" fn fv_settlement_abi_version() -> u32 { 1 }

/// Payload is an unaligned immutable State followed by asset_count Asset records.
#[no_mangle]
pub unsafe extern "C" fn fv_sale_caps(parameters: *const Parameters, payload: *const u8,
                                     length: usize, output: *mut SaleResult) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), i32> {
        if parameters.is_null() || payload.is_null() || output.is_null() || length < std::mem::size_of::<State>() { return Err(2); }
        let state = std::ptr::read_unaligned(payload.cast::<State>());
        if state.asset_count > 200 || length != std::mem::size_of::<State>()+state.asset_count as usize*std::mem::size_of::<Asset>()
                || state.start < 0 || state.end > 718 || state.start >= state.end || state.end-state.start > 144
                || state.capacity > 100 || !state.money.is_finite() || !state.cash_floor.is_finite()
                || state.inventory.iter().any(|v| !v.is_finite() || v.abs() > 1e9)
                || state.consumption.iter().any(|v| !v.is_finite() || v.abs() > 1e6)
                || state.amounts.iter().chain(&state.bags).chain(&state.cargo).chain(&state.upper).any(|v| *v > 10000)
                || state.cargo_origin.abs() > 100000 { return Err(2); }
        let parameters = slice::from_raw_parts(parameters, N);
        if parameters.iter().any(|p| !p.valid()) { return Err(1); }
        let mut assets = Vec::with_capacity(state.asset_count as usize);
        for index in 0..state.asset_count as usize {
            let asset = std::ptr::read_unaligned(payload.add(std::mem::size_of::<State>()+index*std::mem::size_of::<Asset>()).cast::<Asset>());
            if !(1..8).contains(&asset.product) || asset.side < 0 || asset.side > 1 || asset.held < 0
                    || asset.held > 10000 || asset.bonus < 0 || asset.bonus > 10000 || asset.age.abs() > 10000
                    || asset.distance < 0 || asset.distance > 20 { return Err(2); }
            assets.push(asset);
        }
        output.write(run(parameters, &state, &assets, None)?);
        Ok(())
    }));
    match result { Ok(Ok(())) => 0, Ok(Err(code)) => code, Err(_) => 5 }
}

#[no_mangle]
pub extern "C" fn fv_settlement_prediction_abi_version() -> u32 { 1 }

#[no_mangle]
pub unsafe extern "C" fn fv_sale_caps_prediction(parameters: *const Parameters, payload: *const u8,
                                     length: usize, output: *mut SaleResult, prediction_payload: *const u8, prediction_length: usize) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), i32> {
        if parameters.is_null() || payload.is_null() || output.is_null() || length < std::mem::size_of::<State>() { return Err(2); }
        let state = std::ptr::read_unaligned(payload.cast::<State>());
        if state.asset_count > 200 || length != std::mem::size_of::<State>()+state.asset_count as usize*std::mem::size_of::<Asset>()
                || state.start < 0 || state.end > 718 || state.start >= state.end || state.end-state.start > 144
                || state.capacity > 100 || !state.money.is_finite() || !state.cash_floor.is_finite()
                || state.inventory.iter().any(|v| !v.is_finite() || v.abs() > 1e9)
                || state.consumption.iter().any(|v| !v.is_finite() || v.abs() > 1e6)
                || state.amounts.iter().chain(&state.bags).chain(&state.cargo).chain(&state.upper).any(|v| *v > 10000)
                || state.cargo_origin.abs() > 100000 { return Err(2); }
        let parameters = slice::from_raw_parts(parameters, N);
        if parameters.iter().any(|p| !p.valid()) { return Err(1); }
        let mut assets = Vec::with_capacity(state.asset_count as usize);
        for index in 0..state.asset_count as usize {
            let asset = std::ptr::read_unaligned(payload.add(std::mem::size_of::<State>()+index*std::mem::size_of::<Asset>()).cast::<Asset>());
            if !(1..8).contains(&asset.product) || asset.side < 0 || asset.side > 1 || asset.held < 0
                    || asset.held > 10000 || asset.bonus < 0 || asset.bonus > 10000 || asset.age.abs() > 10000
                    || asset.distance < 0 || asset.distance > 20 { return Err(2); }
            assets.push(asset);
        }
        let prediction = Prediction::decode(prediction_payload, prediction_length, &state)?;
        output.write(run(parameters, &state, &assets, Some(&prediction))?);
        Ok(())
    }));
    match result { Ok(Ok(())) => 0, Ok(Err(code)) => code, Err(_) => 5 }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn abi_sizes() {
        assert_eq!(std::mem::size_of::<State>(), 328);
        assert_eq!(std::mem::size_of::<Asset>(), 32);
        assert_eq!(std::mem::size_of::<SaleRecord>(), 56);
        assert_eq!(std::mem::size_of::<SaleResult>(), 552);
    }
}
