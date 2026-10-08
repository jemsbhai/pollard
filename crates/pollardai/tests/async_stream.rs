use futures::{
    executor::block_on,
    future::{pending, ready},
    task::noop_waker,
};
use pollardai::*;
use std::{
    cell::Cell,
    future::Future,
    rc::Rc,
    task::{Context, Poll},
};

#[test]
fn async_calls_gate_dispatch_and_replay_without_polling_provider() {
    block_on(async {
        let rt = AsyncRuntime::memory(ReplayMode::Record);
        let mut run = rt
            .run(
                "async",
                Some(Budget {
                    steps: Some(1),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        let result = run
            .amodel_call(json!({"m":1}), CallOptions::default(), |_| async {
                Ok(json!({"text":"hi","usage":{"input_tokens":2,"output_tokens":3}}))
            })
            .await
            .unwrap();
        assert_eq!(run.spent().unwrap().tokens, 5);
        let called = Cell::new(false);
        assert!(matches!(
            run.amodel_call(json!({"m":2}), CallOptions::default(), |_| async {
                called.set(true);
                Ok(json!({}))
            })
            .await,
            Err(Error::BudgetExceeded { .. })
        ));
        assert!(!called.get());
        let replay =
            AsyncRuntime::from(Runtime::from_shared(rt.shared_store(), ReplayMode::Replay));
        let mut r = replay.run("async", None, 0).unwrap();
        let hit = r
            .amodel_call(json!({"m":1}), CallOptions::default(), |_| async {
                panic!("replay called provider")
            })
            .await
            .unwrap();
        assert_eq!(hit.id, result.id);
    });
}

#[test]
fn cancelled_async_dispatch_settles_unknown_record_and_releases_guard() {
    let rt = Runtime::memory(ReplayMode::Record);
    let mut run = rt.run("cancel", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let mut call = Box::pin(run.amodel_call(
        json!({}),
        CallOptions {
            estimated_tokens: Some(7),
            ..Default::default()
        },
        |_| pending::<Result<Value>>(),
    ));
    let waker = noop_waker();
    assert!(matches!(
        call.as_mut().poll(&mut Context::from_waker(&waker)),
        Poll::Pending
    ));
    assert!(matches!(rt.run("other", None, 0), Err(Error::Busy)));
    drop(call);
    assert!(rt.run("other", None, 0).is_ok());
    let children = rt.store().children(&root).unwrap();
    let pending = rt.store().get(&children[0]).unwrap();
    assert_eq!(pending.meta["state"], "failed");
    assert_eq!(pending.meta["accounting_unknown"], true);
    assert_eq!(pending.meta["charges"]["tokens"], 7);
    let mut retry = rt.run("cancel", None, 0).unwrap();
    assert!(matches!(
        retry.model_call(json!({}), CallOptions::default(), |_| panic!(
            "uncertain redispatch"
        )),
        Err(Error::DuplicateRecording(_))
    ));
}

#[test]
fn synchronous_stream_records_replays_and_settles_failures() {
    let rt = Runtime::memory(ReplayMode::Record);
    let mut run = rt.run("stream", None, 0).unwrap();
    let mut seen = Vec::new();
    let record = run
        .model_stream(
            json!({}),
            CallOptions::default(),
            true,
            |_| {
                Ok(vec![
                    Ok(json!({"delta":{"text":"a"}})),
                    Ok(json!({"delta":{"text":"b"},"unused":true})),
                    Ok(json!({"usage":{"input_tokens":2,"output_tokens":1}})),
                ])
            },
            |c| {
                seen.push(c.clone());
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(record.result.as_ref().unwrap()["text"], "ab");
    assert_eq!(run.spent().unwrap().tokens, 3);
    let replay = Runtime::from_shared(rt.shared_store(), ReplayMode::Replay);
    let mut r = replay.run("stream", None, 0).unwrap();
    let mut emitted = Vec::new();
    r.model_stream(
        json!({}),
        CallOptions::default(),
        true,
        |_| -> Result<Vec<Result<Value>>> { panic!("stream provider on replay") },
        |c| {
            emitted.push(c.clone());
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(seen, emitted);
    let mut fail = rt.run("stream-fail", None, 0).unwrap();
    assert!(fail
        .model_stream(
            json!({}),
            CallOptions {
                estimated_tokens: Some(4),
                ..Default::default()
            },
            false,
            |_| Ok(vec![
                Ok(json!({"text":"partial"})),
                Err(Error::Handler("disconnect".into()))
            ]),
            |_| Ok(())
        )
        .is_err());
    assert_eq!(fail.spent().unwrap().tokens, 4);
    assert_eq!(fail.cursor().unwrap().meta["state"], "failed");
}

#[test]
fn async_stream_replays_retained_chunks() {
    block_on(async {
        let rt = AsyncRuntime::memory(ReplayMode::Record);
        let mut run = rt.run("async-stream", None, 0).unwrap();
        let chunks = Rc::new(std::cell::RefCell::new(
            vec![json!({"text":"a"}), json!({"text":"b"})].into_iter(),
        ));
        let seen = Rc::new(Cell::new(0));
        let record = run
            .amodel_stream(
                json!({}),
                CallOptions::default(),
                true,
                || ready(Ok(chunks.borrow_mut().next())),
                |_| {
                    seen.set(seen.get() + 1);
                    ready(Ok(()))
                },
            )
            .await
            .unwrap();
        assert_eq!(record.result.unwrap()["text"], "ab");
        assert_eq!(seen.get(), 2);
        let replay = Runtime::from_shared(rt.shared_store(), ReplayMode::Replay);
        let mut r = replay.run("async-stream", None, 0).unwrap();
        r.amodel_stream(
            json!({}),
            CallOptions::default(),
            true,
            || {
                panic!("replay pulled stream");
                #[allow(unreachable_code)]
                ready(Ok(None))
            },
            |_| {
                seen.set(seen.get() + 1);
                ready(Ok(()))
            },
        )
        .await
        .unwrap();
        assert_eq!(seen.get(), 4);
    });
}

#[test]
fn async_tools_cannot_bypass_registry() {
    let rt = Runtime::memory(ReplayMode::Record).with_registry(Registry::new(vec![]).unwrap());
    let mut run = rt.run("registry", None, 0).unwrap();
    assert!(block_on(
        run.atool_call("bypass", json!({}), CallOptions::default(), |_| async {
            panic!("bypassed")
        })
    )
    .is_err());
}

#[test]
fn tool_streams_use_tool_identity_and_cannot_bypass_registry() {
    block_on(async {
        let rt = Runtime::memory(ReplayMode::Record);
        let mut run = rt.run("tool-stream", None, 0).unwrap();
        let node = run
            .tool_stream(
                "lookup",
                json!({"id":1}),
                CallOptions::default(),
                true,
                |payload| {
                    assert_eq!(payload, json!({"tool":"lookup","args":{"id":1}}));
                    Ok(vec![Ok(json!({"text":"answer"}))])
                },
                |_| Ok(()),
            )
            .unwrap();
        assert_eq!(node.kind, NodeKind::ToolCall);
        let mut chunks = vec![json!({"text":"second"})].into_iter();
        let node = run
            .atool_stream(
                "lookup",
                json!({"id":2}),
                CallOptions::default(),
                false,
                || ready(Ok(chunks.next())),
                |_| ready(Ok(())),
            )
            .await
            .unwrap();
        assert_eq!(node.kind, NodeKind::ToolCall);
        let governed =
            Runtime::memory(ReplayMode::Record).with_registry(Registry::new(vec![]).unwrap());
        let mut run = governed.run("guarded-stream", None, 0).unwrap();
        assert!(run
            .tool_stream(
                "bypass",
                json!({}),
                CallOptions::default(),
                false,
                |_| -> Result<Vec<Result<Value>>> { panic!("dispatched") },
                |_| Ok(())
            )
            .is_err());
        assert!(run
            .atool_stream(
                "bypass",
                json!({}),
                CallOptions::default(),
                false,
                || {
                    panic!("dispatched");
                    #[allow(unreachable_code)]
                    ready(Ok(None))
                },
                |_| ready(Ok(()))
            )
            .await
            .is_err());
    });
}
