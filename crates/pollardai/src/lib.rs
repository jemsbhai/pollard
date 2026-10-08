//! Native Rust execution, replay and audit runtime for Pollard.
//!
//! Identities use the frozen `pollard/v1` domain and arbitrary-size JSON integers.
//! Runtime callbacks are detached from identity payloads. Strict replay verifies
//! the recording and its ancestors and never dispatches a callback.
//!
//! See the crate README for PyPI 1.6.0 compatibility and remaining integrations.

pub mod adapters;
pub mod mcp;
#[cfg(feature = "nvml")]
mod nvml;
pub mod otel;
#[cfg(feature = "nvml")]
pub use nvml::nvml_energy_meter;
mod aio;
mod decimal;
pub use decimal::{
    exact_add as checked_decimal_add, exact_subtract as checked_decimal_subtract,
    parse_decimal_exact,
};
mod decimal_wire;
pub use decimal_wire::{
    reservation_charges_text, reservation_request_text, TextBudgetReservation,
    TextWindowReservation,
};
mod energy;
#[cfg(feature = "estimate-openai")]
pub mod estimators;
mod hashrope;
mod identity;
#[cfg(feature = "kafka")]
mod kafka;
pub mod kv;
mod merge;
mod meters;
#[cfg(feature = "mongodb")]
mod mongodb;
#[cfg(feature = "neo4j")]
mod neo4j;
#[cfg(feature = "postgres")]
mod postgres;
mod python_display;
#[cfg(feature = "redis")]
pub mod redis;
mod registry;
#[cfg(feature = "mongodb")]
mod remote_worker;
pub mod render;
mod revalidation;
mod runtime;
mod seal;
mod seal_custody;
mod sqlite;
mod store;
pub mod store_spec;
mod stream;
pub mod tokenmaster;

pub use aio::{AsyncRun, AsyncRuntime};
pub use energy::{EnergyCounter, EnergyMeasurement, EnergyMeter, PowerSampler};
pub use hashrope::HashRopeStore;
pub use identity::{
    canonical_bytes, contains_redaction, digest_payload, is_redacted, node_id, redact,
    result_digest_from_text, result_text_and_digest, MAX_SAFE_INTEGER,
};
#[cfg(feature = "kafka")]
pub use kafka::{KafkaOptions, KafkaStore};
pub use merge::{
    export_manifest, export_subtree, gc, import_manifest, import_subtree, merge, ExportReport,
    GCReport, ImportReport, MergeReport,
};
pub use meters::{
    integrate_energy, CostMeter, DepthMeter, MetadataMeter, Meter, MeterMeasurement,
    MeterPrecheckRefusal, ModelPrice, StepMeter, TokenEstimator, TokenMeter, WallClockMeter,
    WindowLimit, WindowMeter,
};
#[cfg(feature = "mongodb")]
pub use mongodb::{MongoBackend, MongoOptions, MongoStore};
#[cfg(feature = "neo4j")]
pub use neo4j::{Neo4jBackend, Neo4jOptions, Neo4jStore};
#[cfg(feature = "postgres")]
pub use postgres::{PostgresOptions, PostgresStore, POSTGRES_SCHEMA_VERSION};
#[cfg(feature = "redis")]
pub use redis::{RedisBackend, RedisConnector, RedisOptions, RedisStore};
pub use registry::{
    resolve_local_refs, schema_has_local_refs, ActionSpec, AsyncHandler, Decision, Handler,
    HandlerFuture, Policy, PolicyContext, Registry,
};
pub use revalidation::*;
pub use runtime::{
    Budget, CallOptions, Charges, MeterBudget, MeterCharges, NodeCallback, ReplayMode,
    RevalidationOptions, Run, RunReport, Runtime, SharedStore,
};
pub use seal::{seal, SealEntry, SealReport, SEAL_ALGORITHM};
pub use seal_custody::{SQLiteSealSink, SealCustodyRecord};
pub use serde_json::{json, Value};
pub use sqlite::{BudgetReservation, ReservationCheck, SQLiteStore, WindowReservation};
pub use store::{
    verify, verify_subtree, LeaseRenewer, MemoryStore, Node, NodeKind, RecordingStore, Store,
    StoreRevision, VerifyFinding, VerifyReport,
};
pub use stream::{consume_stream, consume_stream_async, reemit_chunks, StreamAccumulator};
pub use tokenmaster::{tokenmaster_governance_meters, TokenmasterCostMeter, TokenmasterMeter};

/// Crate result type.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors preserve the refusal/recording identity when one exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Backend {
        detail: String,
        connection_lost: bool,
    },
    ReservationUncertain {
        reservation_id: String,
    },
    SettlementUncertain {
        reservation_id: String,
    },
    Invalid(String),
    Integrity(String),
    MissingRecording(String),
    DuplicateRecording(String),
    NotFound(String),
    BudgetExceeded {
        meter: String,
        node_id: String,
    },
    PolicyViolation {
        detail: String,
        node_id: String,
    },
    ConfirmationRequired {
        token: String,
    },
    UnsupportedSchema(String),
    UsageError {
        node_id: String,
    },
    Busy,
    Handler(String),
    /// A provider may have completed work despite returning an error.
    OutcomeUnknown(Box<Error>),
    MeterPrecheckRefusal(Box<crate::MeterPrecheckRefusal>),
}

impl Error {
    pub fn is_post_dispatch_outcome_unknown(&self) -> bool {
        matches!(self, Self::OutcomeUnknown(_))
    }
}

impl From<MeterPrecheckRefusal> for Error {
    fn from(refusal: MeterPrecheckRefusal) -> Self {
        Self::MeterPrecheckRefusal(Box::new(refusal))
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Backend { detail, .. } => write!(f, "backend error: {detail}"),
            Self::ReservationUncertain { reservation_id } => {
                write!(f, "reservation outcome uncertain: {reservation_id}")
            }
            Self::SettlementUncertain { reservation_id } => {
                write!(f, "settlement outcome uncertain: {reservation_id}")
            }
            Self::Invalid(s) => write!(f, "invalid input: {s}"),
            Self::Integrity(s) => write!(f, "integrity error: {s}"),
            Self::MissingRecording(s) => write!(f, "missing recording: {s}"),
            Self::DuplicateRecording(s) => write!(
                f,
                "recording already exists: {s}; use a new attempt or hybrid mode"
            ),
            Self::NotFound(s) => write!(f, "not found: {s}"),
            Self::BudgetExceeded { meter, node_id } => {
                write!(f, "budget exceeded for {meter}: {node_id}")
            }
            Self::PolicyViolation { detail, node_id } => {
                write!(f, "policy violation: {detail}: {node_id}")
            }
            Self::ConfirmationRequired { token } => write!(f, "confirmation required: {token}"),
            Self::UnsupportedSchema(s) => write!(f, "unsupported schema: {s}"),
            Self::UsageError { node_id } => {
                write!(f, "missing or invalid usage after dispatch: {node_id}")
            }
            Self::Busy => write!(f, "store is executing another runtime operation"),
            Self::Handler(s) => write!(f, "handler failed: {s}"),
            Self::OutcomeUnknown(error) => write!(f, "post-dispatch outcome unknown: {error}"),
            Self::MeterPrecheckRefusal(refusal) => write!(f, "meter refused: {}", refusal.detail),
        }
    }
}

impl std::error::Error for Error {}
