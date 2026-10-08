use pollardai::*;
use std::{
    env,
    time::{Duration, Instant},
};
fn main() {
    let n: usize = env::args()
        .nth(1)
        .unwrap_or_else(|| "200000".into())
        .parse()
        .unwrap();
    assert!(n > 0);
    let rt = Runtime::memory(ReplayMode::Record);
    let mut run = rt.run("stream-memory", None, 0).unwrap();
    let start = Instant::now();
    let mut seen = 0;
    let node = run
        .model_stream(
            json!({"bench":"stream-memory"}),
            CallOptions::default(),
            false,
            |_| Ok((0..n).map(|_| Ok(json!({"result":{"text":"ok"}})))),
            |_| {
                seen += 1;
                Ok(())
            },
        )
        .unwrap();
    let seconds = start.elapsed().as_secs_f64();
    assert_eq!(seen, n);
    assert_eq!(node.result, Some(json!({"text":"ok"})));
    assert_eq!(run.spent().unwrap().steps, 1);
    println!(
        "{}",
        json!({"chunks":n,"seconds":seconds,"node_id":node.id,"result_digest":node.result_digest,"callbacks":seen})
    );
    // Leave a short sampling window for the external peak-working-set monitor.
    std::thread::sleep(Duration::from_millis(100));
}
