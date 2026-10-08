//! Independent append-only custody of published subtree seals.
use crate::identity::{hash, hex64};
use crate::{canonical_bytes, Error, Result, SealReport, SEAL_ALGORITHM};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::path::Path;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SealCustodyRecord {
    pub sequence: i64,
    pub store_id: String,
    pub root_id: String,
    pub algorithm: String,
    pub digest: String,
    pub sealed_at: String,
    pub signer_identity: String,
}
pub struct SQLiteSealSink {
    connection: Connection,
}
fn sql(e: rusqlite::Error) -> Error {
    Error::Integrity(format!("seal custody SQLite: {e}"))
}
impl SQLiteSealSink {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let connection = Connection::open(path).map_err(sql)?;
        connection
            .busy_timeout(Duration::from_secs(30))
            .map_err(sql)?;
        let is_store = connection
            .query_row(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'",
                [],
                |_| Ok(()),
            )
            .optional()
            .map_err(sql)?
            .is_some();
        if is_store {
            return Err(Error::Invalid(
                "seal custody sink must not use a Pollard store database".into(),
            ));
        }
        let tables:Vec<String>=connection.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('seal_custody_schema','seal_custody_records')").map_err(sql)?.query_map([],|r|r.get(0)).map_err(sql)?.collect::<std::result::Result<_,_>>().map_err(sql)?;
        if tables.is_empty() {
            connection.execute_batch("BEGIN IMMEDIATE;
                CREATE TABLE seal_custody_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
                INSERT INTO seal_custody_schema VALUES(1,1);
                CREATE TABLE seal_custody_records(sequence INTEGER PRIMARY KEY AUTOINCREMENT,store_id TEXT NOT NULL,root_id TEXT NOT NULL,algorithm TEXT NOT NULL,digest TEXT NOT NULL,sealed_at TEXT NOT NULL,signer_identity TEXT NOT NULL);
                COMMIT;").map_err(sql)?;
        } else {
            if tables.len() != 2 {
                return Err(Error::Integrity("incomplete seal custody schema".into()));
            }
            let version: Option<i64> = connection
                .query_row(
                    "SELECT version FROM seal_custody_schema WHERE singleton=1",
                    [],
                    |r| r.get(0),
                )
                .optional()
                .map_err(sql)?;
            if version != Some(1) {
                return Err(Error::Integrity(format!(
                    "unsupported seal custody schema version {version:?}"
                )));
            }
            connection.prepare("SELECT sequence,store_id,root_id,algorithm,digest,sealed_at,signer_identity FROM seal_custody_records LIMIT 0").map_err(sql)?;
        }
        connection
            .execute_batch("PRAGMA synchronous=FULL")
            .map_err(sql)?;
        Ok(Self { connection })
    }
    /// Append and durably commit a validated seal. Publication is not a signature.
    pub fn publish(
        &mut self,
        report: &SealReport,
        store_id: &str,
        signer_identity: &str,
        sealed_at: Option<&str>,
    ) -> Result<SealCustodyRecord> {
        if store_id.is_empty() || signer_identity.is_empty() {
            return Err(Error::Invalid(
                "store_id and signer_identity must be nonempty".into(),
            ));
        }
        validate_report(report)?;
        let timestamp = match sealed_at.filter(|s| !s.is_empty()) {
            Some(value) => value.to_owned(),
            None => self
                .connection
                .query_row("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')", [], |r| {
                    r.get::<_, String>(0)
                })
                .map_err(sql)?,
        };
        let transaction = self
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(sql)?;
        transaction.execute("INSERT INTO seal_custody_records(store_id,root_id,algorithm,digest,sealed_at,signer_identity) VALUES(?1,?2,?3,?4,?5,?6)",params![store_id,report.root_id,report.algorithm,report.digest,timestamp,signer_identity]).map_err(sql)?;
        let sequence = transaction.last_insert_rowid();
        transaction.commit().map_err(sql)?;
        Ok(SealCustodyRecord {
            sequence,
            store_id: store_id.into(),
            root_id: report.root_id.clone(),
            algorithm: report.algorithm.clone(),
            digest: report.digest.clone(),
            sealed_at: timestamp,
            signer_identity: signer_identity.into(),
        })
    }
    pub fn records(&self) -> Result<Vec<SealCustodyRecord>> {
        self.connection.prepare("SELECT sequence,store_id,root_id,algorithm,digest,sealed_at,signer_identity FROM seal_custody_records ORDER BY sequence").map_err(sql)?
            .query_map([],|r|Ok(SealCustodyRecord {sequence:r.get(0)?,store_id:r.get(1)?,root_id:r.get(2)?,algorithm:r.get(3)?,digest:r.get(4)?,sealed_at:r.get(5)?,signer_identity:r.get(6)?})).map_err(sql)?.collect::<std::result::Result<_,_>>().map_err(sql)
    }
}
fn validate_report(report: &SealReport) -> Result<()> {
    if report.algorithm != SEAL_ALGORITHM
        || !hex64(&report.root_id)
        || !hex64(&report.digest)
        || report.entries.first().map(|e| e.node_id.as_str()) != Some(report.root_id.as_str())
    {
        return Err(Error::Integrity("invalid seal report identity".into()));
    }
    let mut previous = String::new();
    for (index, entry) in report.entries.iter().enumerate() {
        if index != entry.index
            || !hex64(&entry.node_id)
            || entry.previous.as_deref().unwrap_or("") != previous
            || entry.result_digest.as_deref().is_some_and(|s| !hex64(s))
        {
            return Err(Error::Integrity("invalid seal report chain".into()));
        }
        let record = json!({"index":index,"node_id":entry.node_id,"parent_id":entry.parent_id.as_deref().unwrap_or(""),"kind":entry.kind,"result_digest":entry.result_digest.as_deref().unwrap_or(""),"previous":previous});
        let expected = hash(b"pollard/v1:seal\n", &canonical_bytes(&record)?);
        if expected != entry.seal {
            return Err(Error::Integrity("seal report entry digest mismatch".into()));
        }
        previous = expected;
    }
    if previous != report.digest {
        return Err(Error::Integrity("seal report digest mismatch".into()));
    }
    Ok(())
}
