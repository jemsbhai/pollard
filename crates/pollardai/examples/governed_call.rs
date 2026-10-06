use pollardai::{json, Budget, CallOptions, ReplayMode, Result, Runtime};

fn main() -> Result<()> {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run(
        "demo",
        Some(Budget {
            steps: Some(1),
            tokens: Some(10),
            ..Default::default()
        }),
        0,
    )?;
    let payload = json!({"model":"mock","messages":[{"role":"user","content":"hello"}]});
    let node = run.model_call(
        payload.clone(),
        CallOptions {
            estimated_tokens: Some(5),
            ..Default::default()
        },
        |_| Ok(json!({"text":"hello","usage":{"input_tokens":2,"output_tokens":3}})),
    )?;
    println!("recorded: {}", node.id);
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut run = replay.run("demo", None, 0)?;
    let cached = run.model_call(payload, CallOptions::default(), |_| {
        panic!("strict replay does not call providers")
    })?;
    println!("replayed: {}", cached.id);
    Ok(())
}
