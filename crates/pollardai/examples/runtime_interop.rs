//! Runtime-level contention probe for a live Python call in the same SQLite store.
use pollardai::*;
use std::cell::Cell;

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        return Err(Error::Invalid(
            "usage: runtime_interop DATABASE MODE LABEL".into(),
        ));
    }
    let mode = &args[2];
    let mut runtime = Runtime::new(SQLiteStore::open(&args[1])?, ReplayMode::Record);
    let budget = match mode.as_str() {
        "window" => {
            runtime = runtime.with_meter(WindowMeter::new_decimal("requests", "3", 60.0, None)?);
            None
        }
        "budget" => Some(Budget {
            steps: Some(1),
            ..Default::default()
        }),
        "branch" => Some(Budget {
            steps: Some(10),
            ..Default::default()
        }),
        _ => {
            return Err(Error::Invalid(
                "mode must be window, budget, or branch".into(),
            ))
        }
    };
    let mut run = runtime.run(&args[3], budget, 0)?;
    if mode == "branch" {
        run = run.branch(
            0,
            Some(Budget {
                steps: Some(1),
                ..Default::default()
            }),
        )?;
    }
    let root_id = run.root_id().to_owned();
    let anchor_id = run.cursor_id().to_owned();
    let window_key =
        WindowMeter::new_decimal("requests", "3", 60.0, None)?.window_ledger_key(&root_id)?;
    let dispatched = Cell::new(false);
    let result = run.model_call(
        json!({"source":"rust","case":mode}),
        CallOptions::default(),
        |_| {
            dispatched.set(true);
            Ok(json!({"text":"rust dispatched","usage":{"input_tokens":0,"output_tokens":0}}))
        },
    );
    let status = match result {
        Ok(_) => "completed",
        Err(Error::BudgetExceeded { .. }) => "refused",
        Err(error) => return Err(error),
    };
    println!(
        "{}",
        json!({"mode":mode,"status":status,"dispatched":dispatched.get(),"root_id":root_id,"anchor_id":anchor_id,"window_key":window_key,"cursor":run.cursor()?})
    );
    Ok(())
}
