#![cfg(feature = "redis")]
use pollardai::*;
use rust_decimal::Decimal;
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Barrier,
    },
    time::Duration,
};
fn url() -> String {
    std::env::var("POLLARD_REDIS_TEST_URL")
        .expect("set POLLARD_REDIS_TEST_URL to an isolated test server")
}
fn options(label: &str) -> RedisOptions {
    RedisOptions {
        store_id: format!(
            "rust-parity-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ),
        timeout: Duration::from_secs(3),
        ..Default::default()
    }
}
fn root() -> Node {
    Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"redis café"}),
        None,
        json!({}),
    )
    .unwrap()
}
fn request(limit: u64) -> BudgetReservation {
    BudgetReservation {
        scope_id: "root".into(),
        limits: BTreeMap::from([("steps".into(), Decimal::from(limit))]),
        estimates: BTreeMap::from([("steps".into(), Decimal::ONE)]),
        ..Default::default()
    }
}

#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_crud_raw_result_conflicts_and_runtime_replay() {
    let url = url();
    let options = options("crud");
    let mut store = RedisStore::open_with_options(&url, options.clone()).unwrap();
    let root = root();
    store.put(root.clone()).unwrap();
    let payload: Value =
        serde_json::from_str("{\"huge\":340282366920938463463374607431768211456}").unwrap();
    let child = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        payload,
        Some(json!({"text":"é","tiny":1e-7})),
        json!({}),
    )
    .unwrap();
    store.put(child.clone()).unwrap();
    assert_eq!(store.get(&child.id).unwrap(), child);
    let conflicting = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        child.payload.clone(),
        Some(json!({"text":"different"})),
        json!({}),
    )
    .unwrap();
    store.put(conflicting.clone()).unwrap();
    store.put(conflicting).unwrap();
    assert_eq!(
        store.get(&child.id).unwrap().meta["result_conflicts"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(store.walk(&root.id).unwrap().len(), 2);
    let runtime = Runtime::new(store, ReplayMode::Record);
    let mut run = runtime
        .run(
            "runtime-redis",
            Some(Budget {
                steps: Some(2),
                ..Default::default()
            }),
            0,
        )
        .unwrap();
    let node = run
        .model_call(json!({}), CallOptions::default(), |_| {
            Ok(json!({"usage":{"input_tokens":1,"output_tokens":1}}))
        })
        .unwrap();
    let replay = Runtime::new(
        RedisStore::open_with_options(&url, options).unwrap(),
        ReplayMode::Replay,
    );
    let replayed = replay
        .run("runtime-redis", None, 0)
        .unwrap()
        .model_call(json!({}), CallOptions::default(), |_| {
            panic!("strict replay dispatched")
        })
        .unwrap();
    assert_eq!(node, replayed);
}
#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_independent_clients_arbitrate_exactly_once() {
    let url = url();
    let options = options("races");
    let store = RedisStore::open_with_options(&url, options.clone()).unwrap();
    let barrier = Arc::new(Barrier::new(12));
    let threads = (0..12)
        .map(|i| {
            let url = url.clone();
            let options = options.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let store = RedisStore::open_with_options(&url, options).unwrap();
                barrier.wait();
                let won = store
                    .reserve(&format!("r{i}"), &[request(3)], &[], 30.0)
                    .unwrap()
                    .ok;
                if won {
                    store
                        .settle(
                            &format!("r{i}"),
                            &BTreeMap::from([("steps".into(), Decimal::ONE)]),
                        )
                        .unwrap();
                }
                won
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        threads
            .into_iter()
            .map(|t| usize::from(t.join().unwrap()))
            .sum::<usize>(),
        3
    );
    assert!(!store.reserve("last", &[request(3)], &[], 30.0).unwrap().ok);
}
#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_readonly_missing_namespace_and_schema_loss_never_repair() {
    let url = url();
    let mut options = options("readonly");
    let mut raw = ::redis::Client::open(url.as_str())
        .unwrap()
        .get_connection()
        .unwrap();
    options.create = false;
    options.read_only = true;
    let keys_before = ::redis::cmd("DBSIZE").query::<u64>(&mut raw).unwrap();
    assert!(RedisStore::open_with_options(&url, options.clone()).is_err());
    assert_eq!(
        ::redis::cmd("DBSIZE").query::<u64>(&mut raw).unwrap(),
        keys_before
    );
    options.create = true;
    options.read_only = false;
    let mut store = RedisStore::open_with_options(&url, options.clone()).unwrap();
    let root = root();
    store.put(root.clone()).unwrap();
    options.create = false;
    options.read_only = true;
    let mut readonly = RedisStore::open_with_options(&url, options).unwrap();
    assert!(readonly.put(root).is_err());
    ::redis::cmd("DEL")
        .arg(format!("{}:revision", store.backend().base_key()))
        .query::<u64>(&mut raw)
        .unwrap();
    assert!(store.try_exists("missing").is_err());
    assert!(store.reconnect().is_err());
}
#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_lost_connection_reconnects_and_bad_utf8_fails_closed() {
    let url = url();
    let options = options("reconnect");
    let ids = Arc::new(std::sync::Mutex::new(Vec::<i64>::new()));
    let count = Arc::new(AtomicUsize::new(0));
    let client = ::redis::Client::open(url.as_str()).unwrap();
    let id_copy = ids.clone();
    let count_copy = count.clone();
    let mut store = RedisStore::open_with_connector(
        Arc::new(move || {
            let mut c = client.get_connection().map_err(|e| Error::Backend {
                detail: e.to_string(),
                connection_lost: true,
            })?;
            let id = ::redis::cmd("CLIENT")
                .arg("ID")
                .query::<i64>(&mut c)
                .unwrap();
            id_copy.lock().unwrap().push(id);
            count_copy.fetch_add(1, Ordering::SeqCst);
            Ok(c)
        }),
        options,
    )
    .unwrap();
    let root = root();
    store.put(root.clone()).unwrap();
    let mut raw = ::redis::Client::open(url.as_str())
        .unwrap()
        .get_connection()
        .unwrap();
    ::redis::cmd("CLIENT")
        .arg("KILL")
        .arg("ID")
        .arg(ids.lock().unwrap()[0])
        .query::<u64>(&mut raw)
        .unwrap();
    assert_eq!(store.get(&root.id).unwrap(), root);
    assert!(count.load(Ordering::SeqCst) >= 2);
    ::redis::cmd("HSET")
        .arg(format!("{}:bucket:nodes", store.backend().base_key()))
        .arg("bad")
        .arg(vec![255u8])
        .query::<u64>(&mut raw)
        .unwrap();
    assert!(store.get("bad").is_err());
}
#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_metadata_updates_keep_every_concurrent_patch() {
    let url = url();
    let options = options("patches");
    let mut store = RedisStore::open_with_options(&url, options.clone()).unwrap();
    let root = root();
    store.put(root.clone()).unwrap();
    let threads = (0..8)
        .map(|i| {
            let url = url.clone();
            let options = options.clone();
            let id = root.id.clone();
            std::thread::spawn(move || {
                let mut store = RedisStore::open_with_options(&url, options).unwrap();
                store
                    .update_meta(&id, json!({format!("worker{i}"):i}))
                    .unwrap();
            })
        })
        .collect::<Vec<_>>();
    for thread in threads {
        thread.join().unwrap();
    }
    assert_eq!(
        store.get(&root.id).unwrap().meta.as_object().unwrap().len(),
        8
    );
}

#[test]
fn redis_rejects_url_decoding_overrides_before_connection() {
    for option in ["encoding", "decode_responses", "encoding_errors"] {
        assert!(matches!(
            RedisStore::open(&format!("redis://127.0.0.1:1/?{option}=bad")),
            Err(Error::Invalid(_))
        ));
    }
    for url in [
        "rediss://127.0.0.1:1/#insecure",
        "rediss://127.0.0.1:1/#%69nsecure",
    ] {
        assert!(matches!(RedisStore::open(url), Err(Error::Invalid(_))));
    }
}

#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_revision_overflow_and_wrong_bucket_types_never_partially_commit() {
    let url = url();
    let mut raw = ::redis::Client::open(url.as_str())
        .unwrap()
        .get_connection()
        .unwrap();
    let mut store = RedisStore::open_with_options(&url, options("corruption")).unwrap();
    let base = store.backend().base_key().to_owned();
    ::redis::cmd("SET")
        .arg(format!("{base}:revision"))
        .arg(i64::MAX)
        .query::<()>(&mut raw)
        .unwrap();
    let root = root();
    assert!(store.put(root.clone()).is_err());
    assert!(!store.try_exists(&root.id).unwrap());
    assert_eq!(
        ::redis::cmd("GET")
            .arg(format!("{base}:revision"))
            .query::<i64>(&mut raw)
            .unwrap(),
        i64::MAX
    );
    ::redis::cmd("SET")
        .arg(format!("{base}:revision"))
        .arg(u64::MAX)
        .query::<()>(&mut raw)
        .unwrap();
    assert!(store.try_exists(&root.id).is_err());

    let store = RedisStore::open_with_options(&url, options("wrong-type")).unwrap();
    let base = store.backend().base_key().to_owned();
    let window = WindowReservation {
        ledger_key: "window".into(),
        meter: "steps".into(),
        limit: Decimal::from(2),
        amount: Decimal::ONE,
        window_seconds: 10.0,
    };
    assert!(
        store
            .reserve("settle", &[request(2)], &[window], 30.0)
            .unwrap()
            .ok
    );
    let snapshot = |raw: &mut ::redis::Connection| {
        ["budget", "reservations"]
            .into_iter()
            .map(|bucket| {
                ::redis::cmd("HGETALL")
                    .arg(format!("{base}:bucket:{bucket}"))
                    .query::<BTreeMap<String, String>>(raw)
                    .unwrap()
            })
            .collect::<Vec<_>>()
    };
    let before = snapshot(&mut raw);
    ::redis::cmd("SET")
        .arg(format!("{base}:bucket:window-events"))
        .arg("corrupt")
        .query::<()>(&mut raw)
        .unwrap();
    assert!(store
        .settle("settle", &BTreeMap::from([("steps".into(), Decimal::ONE)]))
        .is_err());
    assert_eq!(snapshot(&mut raw), before);
    assert_eq!(
        ::redis::cmd("GET")
            .arg(format!("{base}:bucket:window-events"))
            .query::<String>(&mut raw)
            .unwrap(),
        "corrupt"
    );
}

// RESP proxy drops an EXEC reply after the real server committed the write.
// This exercises the driver's actual I/O classification and reconnect path.
#[test]
#[ignore = "requires isolated Redis: POLLARD_REDIS_TEST_URL"]
fn redis_lost_exec_reply_retries_committed_reservation_and_settlement() {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::atomic::AtomicBool;
    fn frame(reader: &mut BufReader<TcpStream>) -> std::io::Result<Vec<u8>> {
        let mut line = Vec::new();
        if reader.read_until(b'\n', &mut line)? == 0 {
            return Err(std::io::ErrorKind::UnexpectedEof.into());
        }
        if matches!(line.first(), Some(b'$' | b'*')) {
            let size = std::str::from_utf8(&line[1..line.len() - 2])
                .map_err(|_| std::io::ErrorKind::InvalidData)?
                .parse::<i64>()
                .map_err(|_| std::io::ErrorKind::InvalidData)?;
            if size >= 0 {
                if line[0] == b'$' {
                    let mut body = vec![0; size as usize + 2];
                    reader.read_exact(&mut body)?;
                    line.extend(body);
                } else {
                    for _ in 0..size {
                        line.extend(frame(reader)?);
                    }
                }
            }
        }
        Ok(line)
    }
    let server = url::Url::parse(&url()).unwrap();
    assert!(
        server.username().is_empty() && server.password().is_none(),
        "proxy test requires local test server without credentials"
    );
    let upstream = format!(
        "{}:{}",
        server.host_str().unwrap(),
        server.port().unwrap_or(6379)
    );
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let drop_reply = Arc::new(AtomicBool::new(false));
    let stopping = stop.clone();
    let dropping = drop_reply.clone();
    let proxy = std::thread::spawn(move || {
        let mut clients = Vec::new();
        while !stopping.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((client, _)) => {
                    client.set_nonblocking(false).unwrap();
                    let target = upstream.clone();
                    let drop_reply = dropping.clone();
                    clients.push(std::thread::spawn(move || {
                        let server = TcpStream::connect(target).unwrap();
                        server
                            .set_read_timeout(Some(Duration::from_secs(3)))
                            .unwrap();
                        client
                            .set_read_timeout(Some(Duration::from_secs(3)))
                            .unwrap();
                        let mut source = BufReader::new(client);
                        let mut destination = BufReader::new(server);
                        while let Ok(request) = frame(&mut source) {
                            if destination.get_mut().write_all(&request).is_err() {
                                break;
                            }
                            let reply = match frame(&mut destination) {
                                Ok(reply) => reply,
                                Err(_) => break,
                            };
                            if request == b"*1\r\n$4\r\nEXEC\r\n"
                                && drop_reply.swap(false, Ordering::SeqCst)
                            {
                                break;
                            }
                            if source.get_mut().write_all(&reply).is_err() {
                                break;
                            }
                        }
                    }));
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(2))
                }
                Err(e) => panic!("{e}"),
            }
        }
        for client in clients {
            client.join().unwrap();
        }
    });
    let store = RedisStore::open_with_options(&format!("redis://{address}/"), options("lost-exec"))
        .unwrap();
    drop_reply.store(true, Ordering::SeqCst);
    assert!(store.reserve("retry", &[request(2)], &[], 30.0).unwrap().ok);
    assert!(!drop_reply.load(Ordering::SeqCst));
    drop_reply.store(true, Ordering::SeqCst);
    store
        .settle("retry", &BTreeMap::from([("steps".into(), Decimal::ONE)]))
        .unwrap();
    assert!(!drop_reply.load(Ordering::SeqCst));
    assert!(
        store
            .reserve("second", &[request(2)], &[], 30.0)
            .unwrap()
            .ok
    );
    assert!(!store.reserve("third", &[request(2)], &[], 30.0).unwrap().ok);
    drop(store);
    stop.store(true, Ordering::SeqCst);
    proxy.join().unwrap();
}
