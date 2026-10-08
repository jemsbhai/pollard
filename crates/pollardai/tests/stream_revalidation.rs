use pollardai::*;

fn fixture(label: &str) -> (Runtime, Run, ReplayContract, Value, Node) {
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run(label, None, 0).unwrap();
    let contract = ReplayContract::new("local").unwrap();
    let payload = json!({"model":"local","prompt":"secret input"});
    let original = run
        .model_call(payload.clone(), CallOptions::default(), |_| {
            Ok(json!({"text":"hello","usage":{"input_tokens":1,"output_tokens":1}}))
        })
        .unwrap();
    let root = run.root_id().to_owned();
    run.rollback(&root).unwrap();
    (runtime, run, contract, payload, original)
}

#[test]
fn sync_stream_observation_preserves_golden_and_retains_replay_chunks() {
    let (runtime, mut run, contract, payload, original) = fixture("stream-revalidation");
    let mut seen = vec![];
    let mut options = RevalidationOptions::new("stream-1");
    options.keep_chunks = true;
    let chunks = vec![
        json!({"delta":{"text":"hel"}}),
        json!({"delta":{"text":"lo","usage":{"input_tokens":2,"output_tokens":3}}}),
    ];
    let report = run
        .revalidate_model_stream(
            payload.clone(),
            &contract,
            options,
            |received| {
                assert_eq!(received, payload);
                Ok(chunks.iter().cloned().map(Ok))
            },
            |chunk| {
                seen.push(chunk.clone());
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(seen, chunks);
    assert!(report.matched);
    assert!(!report.exact_match);
    assert_eq!(run.cursor_id(), original.id);
    assert_eq!(
        runtime.store().get(&original.id).unwrap().result,
        original.result
    );
    assert_eq!(run.spent().unwrap().tokens, 7);
    let live = runtime.store().get(&report.live_node_id).unwrap();
    assert_eq!(live.result.as_ref().unwrap()["chunks"], json!(chunks));
    let replay = Runtime::from_shared(runtime.shared_store(), ReplayMode::Replay);
    let mut reader = replay.run("stream-revalidation", None, 0).unwrap();
    let mut replayed = vec![];
    reader
        .model_stream(
            live.payload,
            CallOptions::default(),
            true,
            |_| -> Result<Vec<Result<Value>>> { panic!("replay must not dispatch") },
            |chunk| {
                replayed.push(chunk.clone());
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(replayed, chunks);
}

#[test]
fn async_stream_observation_and_observer_failure_are_accounted_once() {
    futures::executor::block_on(async {
        let (runtime, mut run, contract, payload, original) = fixture("async-stream-revalidation");
        let mut seen = vec![];
        let report = run
            .revalidate_model_stream_async(
                payload.clone(),
                &contract,
                RevalidationOptions::new("async-1"),
                |received| {
                    assert_eq!(received, payload);
                    let mut chunks =
                        vec![json!({"text":"hello","usage":{"input_tokens":2,"output_tokens":3}})]
                            .into_iter();
                    Ok(move || std::future::ready(Ok(chunks.next())))
                },
                |chunk| {
                    seen.push(chunk);
                    std::future::ready(Ok(()))
                },
            )
            .await
            .unwrap();
        assert!(report.matched);
        assert_eq!(seen.len(), 1);
        assert_eq!(run.cursor_id(), original.id);
        assert!(runtime
            .store()
            .get(&report.live_node_id)
            .unwrap()
            .result
            .unwrap()
            .get("chunks")
            .is_none());
        let root = run.root_id().to_owned();
        run.rollback(&root).unwrap();
        let mut options = RevalidationOptions::new("async-failure");
        options.call.estimated_tokens = Some(4);
        let failure = run
            .revalidate_model_stream_async(
                payload,
                &contract,
                options,
                |_| Ok(|| std::future::ready(Ok(Some(json!({"text":"partial"}))))),
                |_| std::future::ready(Err(Error::Handler("observer failed".into()))),
            )
            .await
            .unwrap_err();
        assert!(failure.is_post_dispatch_outcome_unknown());
        assert_eq!(run.spent().unwrap().tokens, 11);
        assert_eq!(run.spent().unwrap().steps, 3);
        assert_eq!(
            runtime.store().get(&original.id).unwrap().result,
            original.result
        );
    });
}

#[test]
fn stream_factory_is_not_constructed_when_revalidation_budget_refuses() {
    futures::executor::block_on(async {
        let runtime = Runtime::memory(ReplayMode::Record);
        let mut run = runtime
            .run(
                "stream-budget",
                Some(Budget {
                    steps: Some(1),
                    ..Default::default()
                }),
                0,
            )
            .unwrap();
        let payload = json!({"model":"local"});
        run.model_call(payload.clone(), CallOptions::default(), |_| Ok(json!({})))
            .unwrap();
        let root = run.root_id().to_owned();
        run.rollback(&root).unwrap();
        let mut factory_called = false;
        let error = run
            .revalidate_model_stream_async(
                payload,
                &ReplayContract::new("local").unwrap(),
                RevalidationOptions::new("blocked"),
                |_| {
                    factory_called = true;
                    Ok(|| std::future::ready(Ok(None)))
                },
                |_| std::future::ready(Ok(())),
            )
            .await
            .unwrap_err();
        assert!(matches!(error, Error::BudgetExceeded { .. }));
        assert!(!factory_called);
    });
}
