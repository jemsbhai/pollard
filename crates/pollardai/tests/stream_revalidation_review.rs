use pollardai::*;
use std::{cell::Cell, future::Future, rc::Rc, task::Context};

#[test]
fn cancelling_after_first_live_chunk_preserves_golden_and_conservative_charges() {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("cancelled-stream-review", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let payload = json!({"model":"test"});
    let original = run
        .model_call(payload.clone(), CallOptions::default(), |_| {
            Ok(json!({"text":"golden", "usage":{"input_tokens":1,"output_tokens":1}}))
        })
        .unwrap();
    run.rollback(&root).unwrap();
    let contract = ReplayContract::new("test").unwrap();
    let mut options = RevalidationOptions::new("cancelled");
    options.call.estimated_tokens = Some(4);
    let seen = Rc::new(Cell::new(0));
    let observer_seen = seen.clone();
    let mut future = Box::pin(run.revalidate_model_stream_async(
        payload.clone(),
        &contract,
        options,
        |_| {
            let mut first = true;
            Ok(move || {
                let emit = first;
                first = false;
                async move {
                    if emit {
                        Ok(Some(json!({"delta":{"text":"partial"}})))
                    } else {
                        std::future::pending().await
                    }
                }
            })
        },
        move |_| {
            observer_seen.set(observer_seen.get() + 1);
            std::future::ready(Ok(()))
        },
    ));
    let mut context = Context::from_waker(futures::task::noop_waker_ref());
    assert!(future.as_mut().poll(&mut context).is_pending());
    assert_eq!(seen.get(), 1);
    drop(future);
    assert_eq!(
        run.spent().unwrap(),
        Charges {
            steps: 2,
            tokens: 6
        }
    );
    assert_eq!(runtime.store().get(&original.id).unwrap(), original);
    let failed = runtime
        .store()
        .walk(&root)
        .unwrap()
        .into_iter()
        .find(|node| node.meta["accounting_unknown"] == true)
        .unwrap();
    assert_eq!(failed.meta["state"], "failed");
    assert!(failed.result.is_none());
    // Failed live dispatch continues from its failure record; only a finished
    // comparison returns to the golden node, as in the Python control flow.
    assert_eq!(run.cursor_id(), failed.id);
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut reader = replay.run("cancelled-stream-review", None, 0).unwrap();
    assert_eq!(
        reader
            .model_call(payload, CallOptions::default(), |_| panic!(
                "golden replay dispatched"
            ))
            .unwrap(),
        original
    );
    reader.rollback(&root).unwrap();
    assert!(reader
        .model_call(failed.payload, CallOptions::default(), |_| panic!(
            "failed observation dispatched"
        ))
        .is_err());
}
