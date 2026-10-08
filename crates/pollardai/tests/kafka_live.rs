#![cfg(feature = "kafka")]
use pollardai::*;
use rdkafka::{
    admin::{AdminClient, AdminOptions, NewTopic, TopicReplication},
    client::DefaultClientContext,
    config::ClientConfig,
};
use std::{
    collections::BTreeMap,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

struct Topic {
    admin: AdminClient<DefaultClientContext>,
    name: String,
}
impl Drop for Topic {
    fn drop(&mut self) {
        let _ = futures::executor::block_on(self.admin.delete_topics(
            &[&self.name],
            &AdminOptions::new().request_timeout(Some(Duration::from_secs(5))),
        ));
    }
}
fn topic(bootstrap: &str, partitions: i32, cleanup: &str, retention: &str) -> Topic {
    let admin: AdminClient<DefaultClientContext> = ClientConfig::new()
        .set("bootstrap.servers", bootstrap)
        .create()
        .unwrap();
    let name = format!(
        "pollard-parity-rust-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let topic = NewTopic::new(&name, partitions, TopicReplication::Fixed(1))
        .set("cleanup.policy", cleanup)
        .set("retention.ms", retention)
        .set("retention.bytes", "-1");
    let result = futures::executor::block_on(admin.create_topics(
        &[topic],
        &AdminOptions::new().request_timeout(Some(Duration::from_secs(10))),
    ))
    .unwrap();
    assert!(result.iter().all(|result| result.is_ok()), "{result:?}");
    Topic { admin, name }
}
fn open(
    bootstrap: &str,
    name: &str,
    read_only: bool,
    require_existing: bool,
) -> Result<KafkaStore> {
    let mut options = KafkaOptions::new(name);
    options.read_only = read_only;
    options.require_existing = require_existing;
    options.timeout = Duration::from_secs(10);
    KafkaStore::open(
        BTreeMap::from([("bootstrap.servers".into(), bootstrap.into())]),
        options,
    )
}

#[test]
#[ignore = "requires POLLARD_TEST_KAFKA_BOOTSTRAP pointing to a disposable broker"]
fn kafka_live_runtime_replay_snapshot_metadata_and_configuration_guards() {
    let bootstrap = std::env::var("POLLARD_TEST_KAFKA_BOOTSTRAP")
        .expect("explicit disposable Kafka bootstrap required");
    let valid = topic(&bootstrap, 1, "delete", "-1");
    assert!(open(&bootstrap, &valid.name, false, true).is_err());
    let runtime = Runtime::new(
        open(&bootstrap, &valid.name, false, false).unwrap(),
        ReplayMode::Record,
    );
    let mut run = runtime.run("live-kafka", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let child = run
        .model_call(json!({"model":"mock"}), CallOptions::default(), |_| {
            Ok(json!({"text":"héllo 雪","usage":{"input_tokens":2,"output_tokens":3}}))
        })
        .unwrap();
    assert_eq!(run.report().unwrap().spent["tokens"], 5.0);
    let observer = open(&bootstrap, &valid.name, true, true).unwrap();
    let replay = Runtime::new(
        open(&bootstrap, &valid.name, true, true).unwrap(),
        ReplayMode::Replay,
    );
    assert_eq!(
        replay
            .run("live-kafka", None, 0)
            .unwrap()
            .model_call(json!({"model":"mock"}), CallOptions::default(), |_| panic!(
                "strict replay provider"
            ))
            .unwrap()
            .result,
        child.result
    );
    let mut writer = open(&bootstrap, &valid.name, false, true).unwrap();
    writer
        .update_meta(&child.id, json!({"reviewed":true}))
        .unwrap();
    assert!(observer
        .get(&child.id)
        .unwrap()
        .meta
        .get("reviewed")
        .is_none());
    observer.reconnect().unwrap();
    assert_eq!(
        observer.get(&child.id).unwrap().meta["reviewed"],
        json!(true)
    );
    assert_eq!(observer.walk(&root).unwrap().len(), 2);
    assert!(verify(&observer, &child.id).ok);
    let records = observer.operation_log().unwrap();
    assert_eq!(
        records
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
            .count(),
        3,
        "root, completed call, metadata patch only"
    );
    drop((writer, observer, replay, run, runtime));
    for (partitions, cleanup, retention) in [
        (2, "delete", "-1"),
        (1, "compact", "-1"),
        (1, "delete", "60000"),
    ] {
        let bad = topic(&bootstrap, partitions, cleanup, retention);
        assert!(open(&bootstrap, &bad.name, false, false).is_err());
    }
    let absent = format!("{}-absent", valid.name);
    assert!(open(&bootstrap, &absent, false, false).is_err());
    assert!(!valid
        .admin
        .inner()
        .fetch_metadata(None, Duration::from_secs(5))
        .unwrap()
        .topics()
        .iter()
        .any(|t| t.name() == absent));
}
