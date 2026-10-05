//! Experimental synchronous native Rust core for Pollard.
//!
//! Identities use the frozen `pollard/v1` domain and portable JSON integers.
//! Runtime callbacks are detached from identity payloads. Strict replay verifies
//! the recording and its ancestors and never dispatches a callback.
//!
//! See the crate README for the deliberately limited 0.1 scope.

mod identity;
mod registry;
mod runtime;
mod store;

pub use identity::{
    canonical_bytes, digest_payload, node_id, redact, result_digest_from_text,
    result_text_and_digest, MAX_SAFE_INTEGER,
};
pub use registry::{ActionSpec, Decision, Handler, Policy, PolicyContext, Registry};
pub use runtime::{Budget, CallOptions, Charges, ReplayMode, Run, Runtime, SharedStore};
pub use serde_json::{json, Value};
pub use store::{
    verify, MemoryStore, Node, NodeKind, RecordingStore, Store, VerifyFinding, VerifyReport,
};

/// Crate result type.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors preserve the refusal/recording identity when one exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Invalid(String),
    Integrity(String),
    MissingRecording(String),
    DuplicateRecording(String),
    NotFound(String),
    BudgetExceeded { meter: String, node_id: String },
    PolicyViolation { detail: String, node_id: String },
    ConfirmationRequired { token: String },
    UnsupportedSchema(String),
    UsageError { node_id: String },
    Busy,
    Handler(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
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
        }
    }
}

impl std::error::Error for Error {}
