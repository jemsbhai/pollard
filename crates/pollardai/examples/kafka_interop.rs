#[cfg(feature = "kafka")]
fn main() -> pollardai::Result<()> {
    use pollardai::*;
    let args: Vec<_> = std::env::args().collect();
    let bootstrap = args.get(1).expect("bootstrap");
    let topic = args.get(2).expect("topic");
    let mode = args.get(3).expect("mode");
    let label = args.get(4).expect("label");
    let mut options = KafkaOptions::new(topic);
    options.read_only = mode == "replay";
    options.require_existing = mode == "replay";
    let store = KafkaStore::open(
        [("bootstrap.servers".into(), bootstrap.clone())].into(),
        options,
    )?;
    let runtime = Runtime::new(
        store,
        if mode == "replay" {
            ReplayMode::Replay
        } else {
            ReplayMode::Record
        },
    );
    let mut run = runtime.run(label, None, 0)?;
    let node = run.model_call(
        json!({"model":"mock","messages":[{"role":"user","content":"héllo"}]}),
        CallOptions::default(),
        |_| {
            assert_ne!(mode, "replay", "replay must not dispatch");
            Ok(json!({"text":"rust","usage":{"input_tokens":7,"output_tokens":3}}))
        },
    )?;
    println!(
        "{}",
        json!({"root_id":run.root_id(),"node_id":node.id,"result":node.result,"meta":node.meta,"report":run.report()?})
    );
    Ok(())
}
#[cfg(not(feature = "kafka"))]
fn main() {
    eprintln!("enable --features kafka");
    std::process::exit(2);
}
