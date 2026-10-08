//! Credential-safe environment references for operator commands. URI/password
//! values remain in the environment and are never part of a displayed label.
use crate::{Error, RecordingStore, Result, SQLiteStore};
use std::{collections::BTreeMap, path::PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteStoreReference {
    pub backend: String,
    pub variable: String,
    pub store_id: String,
    pub parameters: BTreeMap<String, String>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreReference {
    SQLite(PathBuf),
    Remote(RemoteStoreReference),
}
fn invalid(detail: &str) -> Error {
    Error::Invalid(detail.into())
}
fn decode(value: &str, plus: bool) -> Result<String> {
    let mut out = Vec::new();
    let mut bytes = value.bytes();
    while let Some(byte) = bytes.next() {
        if byte == b'%' {
            let hex = |v: u8| {
                char::from(v)
                    .to_digit(16)
                    .ok_or_else(|| invalid("invalid store reference escape"))
            };
            let first = hex(bytes
                .next()
                .ok_or_else(|| invalid("invalid store reference escape"))?)?;
            let second = hex(bytes
                .next()
                .ok_or_else(|| invalid("invalid store reference escape"))?)?;
            out.push((first * 16 + second) as u8);
        } else {
            out.push(if plus && byte == b'+' { b' ' } else { byte });
        }
    }
    String::from_utf8(out).map_err(|_| invalid("store reference must be UTF-8"))
}
fn component(value: &str) -> Result<()> {
    if value.is_empty() || value.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(invalid(
            "store reference component must be nonempty without whitespace or control characters",
        ));
    }
    Ok(())
}
fn variable(value: &str) -> Result<()> {
    component(value)?;
    if !value
        .bytes()
        .enumerate()
        .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
    {
        return Err(invalid(
            "store reference requires an environment variable name",
        ));
    }
    Ok(())
}
impl StoreReference {
    pub fn parse(spec: &str) -> Result<Self> {
        let prefix = spec.split_once(':').map(|(p, _)| p.to_ascii_lowercase());
        let backend = match prefix.as_deref() {
            Some("pg-env") => "postgres",
            Some("redis-env") => "redis",
            Some("mongo-env") => "mongo",
            Some("neo4j-env") => "neo4j",
            Some("kafka-env") => "kafka",
            _ => {
                let decoded = decode(spec, false).unwrap_or_default().to_ascii_lowercase();
                if spec.contains("://") || decoded.contains("://") || decoded.contains("-env:") {
                    return Err(invalid("use an environment-backed store reference; inline remote addresses are not accepted"));
                }
                return Ok(Self::SQLite(PathBuf::from(spec)));
            }
        };
        component(spec)?;
        let tail = spec.split_once(':').expect("prefix").1;
        let (tail, fragment) = tail.split_once('#').unwrap_or((tail, "default"));
        if fragment.contains(['?', '#']) {
            return Err(invalid("store query must precede the single fragment"));
        }
        let store_id = decode(
            if fragment.is_empty() {
                "default"
            } else {
                fragment
            },
            false,
        )?;
        component(&store_id)?;
        let (name, query) = tail.split_once('?').unwrap_or((tail, ""));
        let name = decode(name, false)?;
        variable(&name)?;
        let mut parameters = BTreeMap::new();
        for pair in query.split('&').filter(|_| !query.is_empty()) {
            let (key, value) = pair
                .split_once('=')
                .ok_or_else(|| invalid("store query needs key=value pairs"))?;
            let key = decode(key, true)?;
            let value = decode(value, true)?;
            component(&value)?;
            let allowed = match backend {
                "postgres" => &[][..],
                "redis" => &["prefix"][..],
                "mongo" => &["database", "prefix"][..],
                "neo4j" => &["database", "user-env", "password-env"][..],
                _ => &["topic", "timeout"][..],
            };
            if !allowed.contains(&key.as_str()) || parameters.insert(key, value).is_some() {
                return Err(invalid("unknown or duplicate store query parameter"));
            }
        }
        if backend == "neo4j" {
            for key in ["user-env", "password-env"] {
                variable(
                    parameters
                        .get(key)
                        .ok_or_else(|| invalid("neo4j-env requires user-env and password-env"))?,
                )?;
            }
        }
        if backend == "kafka" {
            if !parameters.contains_key("topic") {
                return Err(invalid("kafka-env requires topic"));
            }
            if let Some(timeout) = parameters.get("timeout") {
                if timeout
                    .parse::<u64>()
                    .map_or(true, |v| v == 0 || v > 2_147_483)
                {
                    return Err(invalid(
                        "Kafka timeout must be positive seconds within the client range",
                    ));
                }
            }
        }
        if backend == "mongo" {
            if let Some(prefix) = parameters.get("prefix") {
                if !prefix.bytes().enumerate().all(|(i, c)| {
                    c.is_ascii_alphabetic() || (i > 0 && (c.is_ascii_digit() || c == b'_'))
                }) {
                    return Err(invalid("Mongo prefix must start with a letter and contain letters, digits or underscores"));
                }
            }
        }
        Ok(Self::Remote(RemoteStoreReference {
            backend: backend.into(),
            variable: name,
            store_id,
            parameters,
        }))
    }
    pub fn label(&self) -> String {
        match self {
            Self::SQLite(path) => path.to_string_lossy().into_owned(),
            Self::Remote(reference) => format!(
                "{}-env:{}#{}",
                if reference.backend == "postgres" {
                    "pg"
                } else {
                    &reference.backend
                },
                reference.variable,
                reference.store_id
            ),
        }
    }
    pub fn open(&self, read_only: bool, create: bool) -> Result<Box<dyn RecordingStore>> {
        match self {
            Self::SQLite(path) => {
                if !create && !path.is_file() {
                    return Err(invalid(
                        "SQLite store does not exist; creation requires --initialize-if-missing",
                    ));
                }
                Ok(Box::new(if read_only {
                    SQLiteStore::open_read_only(path)?
                } else {
                    SQLiteStore::open(path)?
                }))
            }
            Self::Remote(reference) => reference.open(read_only, create).map_err(|_| {
                invalid(&format!(
                    "could not access {}; verify feature, configuration, schema and connection",
                    self.label()
                ))
            }),
        }
    }
}
impl RemoteStoreReference {
    #[allow(unused_variables)]
    fn open(&self, read_only: bool, create: bool) -> Result<Box<dyn RecordingStore>> {
        let get_env = |name: &str| {
            std::env::var(name)
                .ok()
                .filter(|v| !v.is_empty())
                .ok_or_else(|| invalid("required store environment variable is unset"))
        };
        let config = get_env(&self.variable)?;
        let parameter = |name: &str, default: &str| {
            self.parameters
                .get(name)
                .cloned()
                .unwrap_or_else(|| default.to_owned())
        };
        match self.backend.as_str() {
            #[cfg(feature = "postgres")]
            "postgres" => Ok(Box::new(crate::PostgresStore::connect_with_options(
                config,
                crate::PostgresOptions {
                    store_id: self.store_id.clone(),
                    read_only,
                    create,
                    ..Default::default()
                },
            )?)),
            #[cfg(feature = "redis")]
            "redis" => Ok(Box::new(crate::RedisStore::open_with_options(
                &config,
                crate::RedisOptions {
                    store_id: self.store_id.clone(),
                    prefix: parameter("prefix", "pollard"),
                    read_only,
                    create,
                    ..Default::default()
                },
            )?)),
            #[cfg(feature = "mongodb")]
            "mongo" => Ok(Box::new(crate::MongoStore::connect_with_options(
                &config,
                crate::MongoOptions {
                    database: parameter("database", "pollard"),
                    store_id: self.store_id.clone(),
                    collection_prefix: parameter("prefix", "pollard"),
                    read_only,
                    create,
                },
            )?)),
            #[cfg(feature = "neo4j")]
            "neo4j" => Ok(Box::new(crate::Neo4jStore::connect_with_options(
                &config,
                &get_env(&parameter("user-env", ""))?,
                &get_env(&parameter("password-env", ""))?,
                crate::Neo4jOptions {
                    database: parameter("database", "neo4j"),
                    store_id: self.store_id.clone(),
                    read_only,
                    create,
                },
            )?)),
            #[cfg(feature = "kafka")]
            "kafka" => {
                let mut config = client_config(&config)?;
                config.remove("debug");
                config.remove("aws_debug");
                config.insert("log_level".into(), "0".into());
                let mut options = crate::KafkaOptions::new(parameter("topic", ""));
                options.store_id = self.store_id.clone();
                options.read_only = read_only;
                options.require_existing = true;
                options.timeout = std::time::Duration::from_secs(
                    parameter("timeout", "30")
                        .parse()
                        .map_err(|_| invalid("invalid Kafka timeout"))?,
                );
                Ok(Box::new(crate::KafkaStore::open(config, options)?))
            }
            _ => Err(invalid("store backend feature is not enabled")),
        }
    }
}
#[cfg(feature = "kafka")]
fn client_config(raw: &str) -> Result<BTreeMap<String, String>> {
    // A custom visitor rejects duplicate properties before serde_json can erase them.
    struct Unique;
    impl<'de> serde::de::Visitor<'de> for Unique {
        type Value = BTreeMap<String, crate::Value>;
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            write!(f, "a JSON object with unique keys")
        }
        fn visit_map<M: serde::de::MapAccess<'de>>(
            self,
            mut map: M,
        ) -> std::result::Result<Self::Value, M::Error> {
            let mut values = BTreeMap::new();
            while let Some((key, value)) = map.next_entry::<String, crate::Value>()? {
                if values.insert(key, value).is_some() {
                    return Err(serde::de::Error::custom("duplicate client property"));
                }
            }
            Ok(values)
        }
    }
    let mut deserializer = serde_json::Deserializer::from_str(raw);
    let values = serde::de::Deserializer::deserialize_map(&mut deserializer, Unique)
        .map_err(|_| invalid("Kafka configuration must be a JSON object with unique properties"))?;
    deserializer
        .end()
        .map_err(|_| invalid("invalid Kafka client configuration"))?;
    let mut result = BTreeMap::new();
    for (name, value) in values {
        component(&name)?;
        if name == "plugin.library.paths" {
            return Err(invalid("Kafka CLI rejects plugin.library.paths"));
        }
        let text = match value {
            crate::Value::String(text) => text,
            crate::Value::Bool(value) => value.to_string(),
            crate::Value::Number(value) if value.as_f64().is_some_and(f64::is_finite) => {
                value.to_string()
            }
            _ => {
                return Err(invalid(
                    "Kafka properties must be strings, finite numbers or booleans",
                ))
            }
        };
        result.insert(name, text);
    }
    if result
        .get("bootstrap.servers")
        .map_or(true, |v| v.trim().is_empty())
    {
        return Err(invalid("Kafka configuration requires bootstrap.servers"));
    }
    Ok(result)
}
