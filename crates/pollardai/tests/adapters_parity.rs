use pollardai::{adapters::*, json, Value};
use std::cell::Cell;

fn provider(name: &str) -> Provider {
    match name {
        "openai_responses" => Provider::OpenAiResponses,
        "openai_chat" => Provider::OpenAiChat,
        "litellm" => Provider::LiteLlm,
        "anthropic" => Provider::Anthropic,
        "bedrock" => Provider::Bedrock,
        _ => panic!("unknown fixture provider"),
    }
}
#[test]
fn exact_pypi160_provider_responses_and_invalid_usage() {
    let fixture: Value = serde_json::from_str(include_str!("pypi160_adapters.json")).unwrap();
    for (i, case) in fixture["responses"].as_array().unwrap().iter().enumerate() {
        let result = normalize_response(
            provider(case["provider"].as_str().unwrap()),
            case["raw"].clone(),
        );
        if let Some(message) = case.get("error") {
            assert_eq!(
                result.unwrap_err().message,
                message.as_str().unwrap(),
                "case {i}"
            );
        } else {
            assert_eq!(
                result.unwrap(),
                case["expected"],
                "case {i}: {}",
                case["provider"]
            );
        }
    }
}
#[test]
fn exact_pypi160_provider_streams_fragments_usage_and_terminal_errors() {
    let fixture: Value = serde_json::from_str(include_str!("pypi160_adapters.json")).unwrap();
    for (i, case) in fixture["streams"].as_array().unwrap().iter().enumerate() {
        let mut stream = normalize_stream(
            provider(case["provider"].as_str().unwrap()),
            case["raw"].as_array().unwrap().clone(),
        );
        for expected in case["chunks"].as_array().unwrap() {
            assert_eq!(
                stream.next().unwrap().unwrap(),
                *expected,
                "stream {i}: {}",
                case["provider"]
            );
        }
        if let Some(message) = case.get("error") {
            let error = stream.next().unwrap().unwrap_err();
            assert_eq!(error.message, message.as_str().unwrap(), "stream {i}");
            if let Some(event) = case.get("raw_error") {
                assert_eq!(error.raw_event.as_ref(), event);
            }
            if let Some(name) = case.get("event_name") {
                assert_eq!(error.event_name, name.as_str().unwrap());
            }
        }
        assert!(stream.next().is_none(), "stream {i}");
        assert!(stream.next().is_none());
    }
}
#[test]
fn adapter_stream_is_lazy_and_stops_reading_after_error() {
    let reads = Cell::new(0);
    let events = [
        json!({"type":"error","error":{"message":"failure"}}),
        json!({"type":"message_stop"}),
    ];
    let events = events.into_iter().inspect(|_| reads.set(reads.get() + 1));
    let mut stream = normalize_stream(Provider::Anthropic, events);
    assert_eq!(reads.get(), 0);
    assert!(stream.next().unwrap().is_err());
    assert_eq!(reads.get(), 1);
    assert!(stream.next().is_none());
    assert_eq!(reads.get(), 1);
}

#[test]
fn incremental_normalizer_supports_async_producers_without_buffering() {
    let events = [
        json!({"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}),
        json!({"type":"message_stop"}),
    ];
    let expected = normalize_stream(Provider::Anthropic, events.clone())
        .collect::<std::result::Result<Vec<_>, _>>()
        .unwrap();
    let actual = futures::executor::block_on(async {
        let mut normalizer = StreamNormalizer::new(Provider::Anthropic);
        let mut chunks = Vec::new();
        for event in events {
            chunks.push(
                normalizer
                    .push(futures::future::ready(event).await)
                    .unwrap(),
            );
        }
        chunks.extend(normalizer.finish().unwrap());
        assert!(normalizer.finish().unwrap().is_none());
        assert!(normalizer.push(json!({})).is_err());
        chunks
    });
    assert_eq!(actual, expected);
}
#[test]
fn request_merge_preserves_defaults_and_strips_private_identity_metadata() {
    let defaults = json!({"model":"default","store":false,"_pollard":{"private":true}});
    let payload = json!({"model":"requested","_pollard":{"replay_contract":{}}});
    assert_eq!(
        merge_request(&defaults, &payload).unwrap(),
        json!({"model":"requested","store":false})
    );
    assert!(payload.get("_pollard").is_some());
    assert!(merge_request(&Value::Null, &payload).is_err());
    for provider in [
        Provider::Anthropic,
        Provider::Bedrock,
        Provider::LiteLlm,
        Provider::OpenAiChat,
        Provider::OpenAiResponses,
    ] {
        assert!(normalize_response(provider, json!([])).is_err());
    }
}

#[test]
fn provider_failure_after_dispatch_retains_conservative_accounting() {
    use pollardai::{CallOptions, Error, ReplayMode, Runtime};
    let runtime = Runtime::memory(ReplayMode::Record);
    let mut run = runtime.run("adapter-failed", None, 0).unwrap();
    let error = run
        .model_call(
            json!({"model":"fixture"}),
            CallOptions {
                estimated_tokens: Some(7),
                ..Default::default()
            },
            |_| {
                Ok(normalize_response(
                    Provider::OpenAiResponses,
                    json!({"status":"failed","error":{"message":"provider failed"}}),
                )?)
            },
        )
        .unwrap_err();
    assert!(matches!(error, Error::OutcomeUnknown(inner)
        if matches!(inner.as_ref(), Error::Handler(message) if message == "provider failed")));
    let spent = run.spent().unwrap();
    assert_eq!(spent.steps, 1);
    assert_eq!(spent.tokens, 7);
    assert!(run.cursor().unwrap().result.is_none());
}
