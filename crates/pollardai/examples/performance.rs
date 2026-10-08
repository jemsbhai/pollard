//! Comparable, dependency-free kernels shared with the published 0.1.0 core.
use pollardai::*;
use std::{env, hint::black_box, time::Instant};

fn payload(i: usize) -> Value {
    json!({"model":"offline","prompt":"Summarize this local deterministic benchmark.","i":i})
}
fn response() -> Value {
    json!({"text":"ok","usage":{"input_tokens":2,"output_tokens":3}})
}
fn options() -> CallOptions {
    CallOptions {
        estimated_tokens: Some(5),
        ..Default::default()
    }
}
fn populate(n: usize) -> (Runtime, String) {
    let rt = Runtime::memory(ReplayMode::Record);
    let mut run = rt.run("bench", None, 0).unwrap();
    for i in 0..n {
        run.model_call(payload(i), options(), |_| Ok(response()))
            .unwrap();
    }
    let root = run.root_id().to_string();
    (rt, root)
}
fn sample(operation: &str, n: usize) -> (f64, String) {
    match operation {
        "identity" => {
            let p = payload(0);
            let start = Instant::now();
            let mut digest = String::new();
            for _ in 0..n {
                digest = black_box(node_id("model_call", None, 0, black_box(&p)).unwrap());
            }
            (start.elapsed().as_secs_f64(), digest)
        }
        "record" => {
            let rt = Runtime::memory(ReplayMode::Record);
            let mut run = rt.run("bench", None, 0).unwrap();
            let start = Instant::now();
            for i in 0..n {
                black_box(
                    run.model_call(payload(i), options(), |_| Ok(response()))
                        .unwrap(),
                );
            }
            let elapsed = start.elapsed().as_secs_f64();
            assert_eq!(run.spent().unwrap().steps, n as u64);
            assert_eq!(run.spent().unwrap().tokens, 5 * n as u64);
            (elapsed, run.cursor_id().to_owned())
        }
        "replay" | "hybrid" => {
            let (rt, _) = populate(n);
            let replay = Runtime::from_shared(
                rt.shared_store(),
                if operation == "replay" {
                    ReplayMode::Replay
                } else {
                    ReplayMode::Hybrid
                },
            );
            let mut run = replay.run("bench", None, 0).unwrap();
            let start = Instant::now();
            for i in 0..n {
                black_box(
                    run.model_call(payload(i), options(), |_| {
                        panic!("provider called during replay")
                    })
                    .unwrap(),
                );
            }
            let elapsed = start.elapsed().as_secs_f64();
            assert_eq!(run.spent().unwrap().steps, n as u64);
            assert_eq!(run.spent().unwrap().tokens, 5 * n as u64);
            (elapsed, run.cursor_id().to_owned())
        }
        "walk" => {
            let mut store = MemoryStore::new();
            let root = Node::make(
                NodeKind::Root,
                None,
                0,
                json!({"run":"wide"}),
                None,
                json!({}),
            )
            .unwrap();
            store.put(root.clone()).unwrap();
            for i in 0..n {
                store
                    .put(
                        Node::make(
                            NodeKind::Note,
                            Some(&root.id),
                            0,
                            json!({"i":i}),
                            None,
                            json!({}),
                        )
                        .unwrap(),
                    )
                    .unwrap();
            }
            let start = Instant::now();
            let nodes = black_box(store.walk(&root.id).unwrap());
            let elapsed = start.elapsed().as_secs_f64();
            assert_eq!(nodes.len(), n + 1);
            assert_eq!(nodes[0].id, root.id);
            assert_eq!(
                nodes
                    .iter()
                    .map(|node| &node.id)
                    .collect::<std::collections::HashSet<_>>()
                    .len(),
                n + 1
            );
            assert!(nodes[1..]
                .iter()
                .all(|node| node.parent.as_deref() == Some(root.id.as_str())));
            (
                elapsed,
                result_digest_from_text(
                    &nodes
                        .iter()
                        .map(|node| node.id.as_str())
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
            )
        }
        _ => panic!("unknown benchmark"),
    }
}
fn main() {
    let args: Vec<String> = env::args().collect();
    let operation = args.get(1).map(String::as_str).unwrap_or("record");
    let n = args.get(2).and_then(|n| n.parse().ok()).unwrap_or(1000);
    let samples = args.get(3).and_then(|n| n.parse().ok()).unwrap_or(7);
    assert!(n > 0 && samples > 0);
    for _ in 0..2 {
        sample(operation, n);
    }
    let mut seconds = Vec::new();
    let mut checksum = String::new();
    let mut checksums = Vec::new();
    for _ in 0..samples {
        let (elapsed, result) = sample(operation, n);
        seconds.push(elapsed);
        checksum = result;
        checksums.push(checksum.clone());
    }
    println!(
        "{}",
        json!({"operation":operation,"size":n,"samples_seconds":seconds,"checksum":checksum,"checksums":checksums,"warmups":2})
    );
}
