#[cfg(not(any(feature = "mongodb", feature = "neo4j")))]
fn main() {
    eprintln!("enable mongodb or neo4j");
    std::process::exit(2);
}
#[cfg(any(feature = "mongodb", feature = "neo4j"))]
fn main() -> pollardai::Result<()> {
    use pollardai::*;
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 5 {
        return Err(Error::Invalid(
            "usage: document_interop BACKEND STORE MODE LABEL_OR_ID".into(),
        ));
    }
    let env =
        |name: &str| std::env::var(name).map_err(|_| Error::Invalid(format!("{name} required")));
    match a[1].as_str() {
        #[cfg(feature = "mongodb")]
        "mongodb" => work(
            MongoStore::connect_with_options(
                &env("POLLARD_TEST_MONGO_URI")?,
                MongoOptions {
                    database: env("POLLARD_TEST_MONGO_DATABASE")?,
                    store_id: a[2].clone(),
                    create: false,
                    ..Default::default()
                },
            )?,
            &a[1..],
        ),
        #[cfg(feature = "neo4j")]
        "neo4j" => work(
            Neo4jStore::connect_with_options(
                &env("POLLARD_TEST_NEO4J_URI")?,
                &env("POLLARD_TEST_NEO4J_USER")?,
                &env("POLLARD_TEST_NEO4J_PASSWORD")?,
                Neo4jOptions {
                    store_id: a[2].clone(),
                    create: false,
                    ..Default::default()
                },
            )?,
            &a[1..],
        ),
        _ => Err(Error::Invalid("backend feature not enabled".into())),
    }
}
#[cfg(any(feature = "mongodb", feature = "neo4j"))]
fn work<B: pollardai::kv::KvBackend>(
    store: pollardai::kv::TransactionalKvStore<B>,
    args: &[String],
) -> pollardai::Result<()> {
    use pollardai::*;
    use rust_decimal::Decimal;
    use std::collections::BTreeMap;
    if args[2] == "export" {
        println!("{}", export_manifest(&store, &args[3])?);
        return Ok(());
    }
    if args[2] == "settle" || args[2] == "settle-tiny" {
        store.settle(
            &args[3],
            &BTreeMap::from([(
                "usd".into(),
                Decimal::new(3, if args[2] == "settle-tiny" { 8 } else { 1 }),
            )]),
        )?;
        println!("{}", json!({"settled":true}));
        return Ok(());
    }
    let mut runtime = Runtime::new(store, ReplayMode::Record);
    let budget = if args[2] == "window" {
        runtime = runtime.with_meter(WindowMeter::new_decimal("requests", "3", 60.0, None)?);
        None
    } else if args[2] == "budget" {
        Some(Budget {
            steps: Some(1),
            ..Default::default()
        })
    } else {
        None
    };
    let mut run = runtime.run(&args[3], budget, 0)?;
    let mut dispatched = false;
    let value = run.model_call(json!({"source":"rust"}), CallOptions::default(), |_| {
        dispatched = true;
        Ok(json!({"float":1e-5,"usage":{"input_tokens":2,"output_tokens":3}}))
    });
    let status = match value {
        Ok(_) => "completed",
        Err(Error::BudgetExceeded { .. }) => "refused",
        Err(error) => return Err(error),
    };
    println!(
        "{}",
        json!({"status":status,"dispatched":dispatched,"root_id":run.root_id(),"node_id":run.cursor_id()})
    );
    Ok(())
}
