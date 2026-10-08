#[cfg(not(feature = "postgres"))]
fn main() {
    eprintln!("enable the postgres feature");
    std::process::exit(2);
}

#[cfg(feature = "postgres")]
fn main() -> pollardai::Result<()> {
    use pollardai::*;
    use rust_decimal::Decimal;
    use std::collections::BTreeMap;
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        return Err(Error::Invalid(
            "usage: postgres_interop STORE MODE LABEL_OR_ID (DSN from POLLARD_TEST_POSTGRES_DSN)"
                .into(),
        ));
    }
    let dsn = std::env::var("POLLARD_TEST_POSTGRES_DSN")
        .map_err(|_| Error::Invalid("POLLARD_TEST_POSTGRES_DSN required".into()))?;
    let store = PostgresStore::connect_with_options(
        dsn,
        PostgresOptions {
            store_id: args[1].clone(),
            create: false,
            ..Default::default()
        },
    )?;
    if args[2] == "export" {
        println!("{}", export_manifest(&store, &args[3])?);
        return Ok(());
    }
    if args[2] == "settle" {
        store.settle(
            &args[3],
            &BTreeMap::from([("usd".into(), Decimal::new(3, 1))]),
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
