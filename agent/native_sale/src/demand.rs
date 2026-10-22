//! Exact marginal demand repricing with stable state insertion order.
use super::*;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Trade { tick: i32, group: i32, side: i32, sign: i32 }

#[derive(Clone, Copy)]
struct State { inventory: f64, rate: i32, probability: f64 }

fn add(states: &mut Vec<State>, index: &mut HashMap<(u64, i32), usize>, state: State) {
    let key = (bits(state.inventory), state.rate);
    if let Some(&i) = index.get(&key) {
        states[i].probability += state.probability;
    } else {
        index.insert(key, states.len());
        states.push(state);
    }
}

#[no_mangle]
pub unsafe extern "C" fn fv_reprice(parameters: *const Parameters, inventory: f64,
    start: i32, seat: i32, known_rate: i32, unlock: i32, unknown: i32,
    interval: i32, center: i32, has_center: i32,
    rates: *const i32, probabilities: *const f64, rate_count: usize,
    tape: *const Trade, tape_count: usize, output: *mut f64) -> i32 {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<(), i32> {
        if parameters.is_null() || rates.is_null() || probabilities.is_null() || tape.is_null() || output.is_null()
                || !(1..=8).contains(&rate_count) || tape_count == 0 || tape_count > 1000000
                || !(0..=718).contains(&start) || !(0..2).contains(&seat) || unlock < 1
                || interval < 1 || center < 1 || !(0..=8).contains(&unknown)
                || !inventory.is_finite() || inventory.abs() > 1e9 { return Err(2); }
        let p = *parameters;
        if !p.valid() { return Err(1); }
        let rates = slice::from_raw_parts(rates, rate_count);
        let probabilities = slice::from_raw_parts(probabilities, rate_count);
        let tape = slice::from_raw_parts(tape, tape_count);
        if rates.iter().any(|v| !(0..=2).contains(v)) || probabilities.iter().any(|v| !v.is_finite() || *v < 0.0 || *v > 1.0)
                || tape.iter().any(|t| t.tick < start || t.tick > 718 || !(0..2).contains(&t.side) || ![-1,1].contains(&t.sign))
                || tape.windows(2).any(|w| w[0].tick > w[1].tick || (w[0].tick == w[1].tick && w[0].group > w[1].group)) { return Err(2); }
        BOOKS.with(|books| -> Result<(), i32> {
            let mut books = books.borrow_mut();
            if !books.contains_key(&p.key()) && books.len() >= 36 { books.clear(); }
            let book = books.entry(p.key()).or_insert_with(|| PriceLadder::new(p));
            let mut states = vec![State {inventory, rate: 0, probability: 1.0}];
            let mut earned = [0.0, 0.0];
            let mut floor_units = 0.0;
            let mut peak = 1;
            let mut offset = 0;
            let first_node = (start/unlock+1)*unlock;
            for tick in start..=tape.last().unwrap().tick {
                if tick >= first_node && (tick-first_node)%unlock == 0 && (tick-first_node)/unlock < unknown {
                    let mut branches = Vec::new();
                    let mut index = HashMap::new();
                    for state in &states {
                        for (&rate, &weight) in rates.iter().zip(probabilities) {
                            add(&mut branches, &mut index, State {inventory: state.inventory,
                                rate: state.rate+rate, probability: state.probability*weight});
                        }
                    }
                    states = branches;
                }
                while offset < tape.len() && tape[offset].tick == tick {
                    let begin = offset;
                    let group = tape[offset].group;
                    while offset < tape.len() && tape[offset].tick == tick && tape[offset].group == group { offset += 1; }
                    let mut branches = Vec::with_capacity(states.len());
                    let mut index = HashMap::new();
                    for state in &states {
                        let mut change = 0;
                        for trade in &tape[begin..offset] {
                            let price = book.quote.price(state.inventory-if trade.sign == 1 {0.0} else {1.0});
                            earned[trade.side as usize] += state.probability*trade.sign as f64*price as f64;
                            if trade.sign == 1 {
                                change += i32::from(price > 1);
                                if trade.side == seat && price == 1 { floor_units += state.probability; }
                            } else { change -= 1; }
                        }
                        add(&mut branches, &mut index, State {inventory: state.inventory+change as f64, ..*state});
                    }
                    states = branches;
                }
                if tick%interval == 0 || tick%center == 0 {
                    // Translation at fixed rate is injective; no probability merge is possible.
                    for state in &mut states {
                        let demand = if tick%interval == 0 {known_rate+state.rate} else {0}
                            +i32::from(tick%center == 0 && has_center != 0);
                        state.inventory -= demand as f64;
                    }
                }
                peak = peak.max(states.len());
                if states.len() > 1000000 { return Err(2); }
            }
            if (states.iter().map(|s| s.probability).sum::<f64>()-1.0).abs() > 1e-8 { return Err(3); }
            slice::from_raw_parts_mut(output, 5).copy_from_slice(&[earned[0],earned[1],floor_units,states.len() as f64,peak as f64]);
            Ok(())
        })
    }));
    match result { Ok(Ok(())) => 0, Ok(Err(code)) => code, Err(_) => 5 }
}
