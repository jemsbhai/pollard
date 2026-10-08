use pollardai::store_spec::*;
#[test]
fn namespace_references_parse_without_loading_credentials() {
    for (spec, backend, var, store) in [
        (
            "pg-env:POLLARD_TEST_PG#one",
            "postgres",
            "POLLARD_TEST_PG",
            "one",
        ),
        (
            "redis-env:REDIS_URL?prefix=app#three",
            "redis",
            "REDIS_URL",
            "three",
        ),
        (
            "mongo-env:MONGO_URI?database=recordings&prefix=app#four",
            "mongo",
            "MONGO_URI",
            "four",
        ),
        (
            "neo4j-env:NEO_URI?user-env=NEO_USER&password-env=NEO_PASSWORD&database=neo4j#five",
            "neo4j",
            "NEO_URI",
            "five",
        ),
        (
            "kafka-env:KAFKA_CONFIG?topic=recordings&timeout=15#six",
            "kafka",
            "KAFKA_CONFIG",
            "six",
        ),
        (
            "redis-env:REDIS_URL#percent%2Fid",
            "redis",
            "REDIS_URL",
            "percent/id",
        ),
    ] {
        let StoreReference::Remote(reference) = StoreReference::parse(spec).unwrap() else {
            panic!("remote reference");
        };
        assert_eq!(
            (
                &*reference.backend,
                &*reference.variable,
                &*reference.store_id
            ),
            (backend, var, store)
        );
    }
    for local in [
        "recording.db",
        r"C:\Users\User\recording.db",
        "recordings with spaces.db",
    ] {
        assert!(matches!(
            StoreReference::parse(local).unwrap(),
            StoreReference::SQLite(_)
        ));
    }
}
#[test]
fn selectors_reject_credentials_obfuscation_and_ambiguous_parameters() {
    for spec in [
        "postgres://user:secret@host/db",
        "redis://user:secret@host/0",
        "mongodb://user:secret@host",
        "bolt://user:secret@host",
        "kafka://host",
        "redis%2Denv:VAR",
        "%72edis-env:VAR",
        " REDIS-env:VAR",
        "redis-env://HOST",
        "redis-env:VAR?prefix=x&prefix=y",
        "mongo-env:VAR?unknown=secret",
        "mongo-env:VAR?prefix=1bad",
        "redis-env:VAR?prefix=x&&",
        "redis-env:VAR?prefix=%xx",
        "redis-env:VAR#x?prefix=y",
        "redis-env:VAR?prefix=%20",
        "neo4j-env:VAR",
        "neo4j-env:VAR?user-env=USER&password-env=",
        "kafka-env:VAR",
        "kafka-env:VAR?topic=one&timeout=0",
        "kafka-env:VAR?topic=one&timeout=-1",
        "kafka-env:VAR?topic=one&timeout=999999999999999999999",
        "redis-env:VAR%00",
    ] {
        let error = StoreReference::parse(spec).unwrap_err().to_string();
        assert!(!error.contains("secret"), "{spec}: {error}");
    }
}
#[test]
fn unset_remote_references_fail_without_disclosing_values() {
    let reference = StoreReference::parse("pg-env:POLLARD_RUST_TEST_UNSET_43858721#safe").unwrap();
    let error = match reference.open(true, false) {
        Ok(_) => panic!("missing environment must fail"),
        Err(error) => error,
    };
    assert!(error
        .to_string()
        .contains("pg-env:POLLARD_RUST_TEST_UNSET_43858721#safe"));
}
