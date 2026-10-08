#[cfg(feature = "redis")]
fn main() -> pollardai::Result<()> {
    use pollardai::*;
    use rust_decimal::Decimal;
    use std::{collections::BTreeMap, str::FromStr};
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let command = args.first().map(String::as_str).unwrap_or("");
    let store_id = args
        .get(1)
        .ok_or_else(|| Error::Invalid("usage: redis_interop command store-id [id]".into()))?;
    let url = std::env::var("POLLARD_REDIS_TEST_URL")
        .map_err(|_| Error::Invalid("POLLARD_REDIS_TEST_URL missing".into()))?;
    let mut store = RedisStore::open_with_options(
        &url,
        RedisOptions {
            store_id: store_id.clone(),
            create: command == "seed",
            ..Default::default()
        },
    )?;
    let id = args.get(2).map(String::as_str).unwrap_or("");
    let dec = |s: &str| Decimal::from_str(s).unwrap();
    match command {
        "reserve-text" => {
            let amount = args
                .get(3)
                .ok_or_else(|| Error::Invalid("decimal amount missing".into()))?;
            let map = |value: &str| BTreeMap::from([("usd".into(), value.into())]);
            let budget = TextBudgetReservation {
                scope_id: "decimal-scope".into(),
                limits: map("1E+6"),
                baseline: map("1E+2"),
                estimates: map(amount),
            };
            let window = TextWindowReservation {
                ledger_key: "decimal-window".into(),
                meter: "usd".into(),
                limit: "1.0E+6".into(),
                amount: amount.clone(),
                window_seconds: 60.0,
            };
            println!(
                "{}",
                store
                    .reserve_decimal_text(id, &[budget], &[window], 60.0)?
                    .ok
            );
        }
        "settle-text" => {
            let amount = args
                .get(3)
                .ok_or_else(|| Error::Invalid("decimal amount missing".into()))?;
            store.settle_decimal_text(id, &BTreeMap::from([("usd".into(), amount.clone())]))?;
            println!("settled");
        }
        "seed" => {
            let root = Node::make(
                NodeKind::Root,
                None,
                0,
                json!({"run":"rust Redis interop","huge":340282366920938463463374607431768211455u128}),
                None,
                json!({"origin":"rust"}),
            )?;
            store.put(root.clone())?;
            store.put(Node::make(
                NodeKind::ModelCall,
                Some(&root.id),
                0,
                json!({"model":"test"}),
                Some(
                    json!({"text":"雪é","float":1e-7,"usage":{"input_tokens":2,"output_tokens":3}}),
                ),
                json!({}),
            )?)?;
            println!("{}", root.id);
        }
        "inspect" => {
            println!(
                "{}",
                json!(store
                    .walk(id)?
                    .iter()
                    .map(kv::node_text)
                    .collect::<Result<Vec<_>>>()?)
            );
        }
        "reserve" => {
            let b = BudgetReservation {
                scope_id: "interop-scope".into(),
                limits: BTreeMap::from([
                    ("steps".into(), dec("2")),
                    ("usd".into(), dec("0.0000010")),
                ]),
                baseline: BTreeMap::from([
                    ("steps".into(), dec("0")),
                    ("usd".into(), dec("0.00000000")),
                ]),
                estimates: BTreeMap::from([
                    ("steps".into(), dec("1")),
                    ("usd".into(), dec("0.0000002")),
                ]),
            };
            let w = WindowReservation {
                ledger_key: "interop-window".into(),
                meter: "steps".into(),
                limit: dec("2"),
                amount: dec("1"),
                window_seconds: 10.0,
            };
            println!("{}", store.reserve(id, &[b], &[w], 30.0)?.ok);
        }
        "settle" => {
            store.settle(
                id,
                &BTreeMap::from([
                    ("steps".into(), dec("1")),
                    ("usd".into(), dec("0.00000010")),
                ]),
            )?;
            println!("settled");
        }
        "release" => {
            store.release(id)?;
            println!("released");
        }
        _ => return Err(Error::Invalid("unknown Redis interop command".into())),
    }
    Ok(())
}
#[cfg(not(feature = "redis"))]
fn main() {
    eprintln!("build with --features redis");
}
