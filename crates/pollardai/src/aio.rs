//! Executor-independent, single-threaded async calls with the same dispatch gates.
use crate::{
    consume_stream, json, reemit_chunks, runtime::CallPreparation, Budget, CallOptions, Node,
    NodeKind, RecordingStore, ReplayMode, Result, Run, Runtime, Value,
};
use std::{
    future::Future,
    ops::{Deref, DerefMut},
};

#[derive(Clone)]
pub struct AsyncRuntime {
    inner: Runtime,
}
impl From<Runtime> for AsyncRuntime {
    fn from(inner: Runtime) -> Self {
        Self { inner }
    }
}
impl Deref for AsyncRuntime {
    type Target = Runtime;
    fn deref(&self) -> &Runtime {
        &self.inner
    }
}
impl AsyncRuntime {
    pub fn new(store: impl RecordingStore + 'static, mode: ReplayMode) -> Self {
        Runtime::new(store, mode).into()
    }
    pub fn memory(mode: ReplayMode) -> Self {
        Runtime::memory(mode).into()
    }
    pub fn run(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        attempt: u64,
    ) -> Result<AsyncRun> {
        Ok(self.inner.run(label, budget, attempt)?.into())
    }
    pub fn resume(
        &self,
        label: impl Into<String>,
        budget: Option<Budget>,
        attempt: u64,
    ) -> Result<AsyncRun> {
        Ok(self.inner.resume(label, budget, attempt)?.into())
    }
}
pub struct AsyncRun {
    inner: Run,
}
impl From<Run> for AsyncRun {
    fn from(inner: Run) -> Self {
        Self { inner }
    }
}
impl Deref for AsyncRun {
    type Target = Run;
    fn deref(&self) -> &Run {
        &self.inner
    }
}
impl DerefMut for AsyncRun {
    fn deref_mut(&mut self) -> &mut Run {
        &mut self.inner
    }
}

impl Run {
    /// Await a model callback only after replay, identity and budget checks.
    /// Dropping the future after dispatch settles a conservative unknown-outcome
    /// recording, so uncertainty cannot silently become a reusable result.
    pub async fn amodel_call<F, Fut>(
        &mut self,
        payload: Value,
        options: CallOptions,
        handler: F,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Fut,
        Fut: Future<Output = Result<Value>>,
    {
        let _guard = self.operation_lock()?;
        match self.prepare_call(NodeKind::ModelCall, payload.clone(), options)? {
            CallPreparation::Recorded(node) => Ok(node),
            CallPreparation::Dispatch(dispatch) => {
                let outcome = handler(payload).await;
                self.finish_call(dispatch, outcome)
            }
        }
    }
    /// Await an unfenced tool. Registered runtimes reject this escape hatch.
    pub async fn atool_call<F, Fut>(
        &mut self,
        name: &str,
        args: Value,
        options: CallOptions,
        handler: F,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Fut,
        Fut: Future<Output = Result<Value>>,
    {
        let _guard = self.operation_lock()?;
        self.ensure_unfenced_tool()?;
        if !args.is_object() {
            return Err(crate::Error::Invalid("tool args must be object".into()));
        }
        let payload = json!({"tool":name,"args":args});
        match self.prepare_call(NodeKind::ToolCall, payload.clone(), options)? {
            CallPreparation::Recorded(node) => Ok(node),
            CallPreparation::Dispatch(dispatch) => {
                let outcome = handler(payload).await;
                self.finish_call(dispatch, outcome)
            }
        }
    }

    /// Consume a native iterator of chunks. Replay reemits retained snapshots.
    pub fn model_stream<F, I, O>(
        &mut self,
        payload: Value,
        options: CallOptions,
        keep_chunks: bool,
        handler: F,
        on_delta: O,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<I>,
        I: IntoIterator<Item = Result<Value>>,
        O: FnMut(&Value) -> Result<()>,
    {
        self.stream_call(
            NodeKind::ModelCall,
            payload,
            options,
            keep_chunks,
            handler,
            on_delta,
        )
    }

    pub fn tool_stream<F, I, O>(
        &mut self,
        name: &str,
        args: Value,
        options: CallOptions,
        keep_chunks: bool,
        handler: F,
        on_delta: O,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<I>,
        I: IntoIterator<Item = Result<Value>>,
        O: FnMut(&Value) -> Result<()>,
    {
        if !args.is_object() {
            return Err(crate::Error::Invalid("tool args must be object".into()));
        }
        self.stream_call(
            NodeKind::ToolCall,
            json!({"tool":name,"args":args}),
            options,
            keep_chunks,
            handler,
            on_delta,
        )
    }

    fn stream_call<F, I, O>(
        &mut self,
        kind: NodeKind,
        payload: Value,
        options: CallOptions,
        keep_chunks: bool,
        handler: F,
        mut on_delta: O,
    ) -> Result<Node>
    where
        F: FnOnce(Value) -> Result<I>,
        I: IntoIterator<Item = Result<Value>>,
        O: FnMut(&Value) -> Result<()>,
    {
        let _guard = self.operation_lock()?;
        if kind == NodeKind::ToolCall {
            self.ensure_unfenced_tool()?;
        }
        match self.prepare_call(kind, payload.clone(), options)? {
            CallPreparation::Recorded(node) => {
                if let Some(result) = &node.result {
                    reemit_chunks(result, on_delta)?;
                }
                Ok(node)
            }
            CallPreparation::Dispatch(dispatch) => {
                let outcome = handler(payload)
                    .and_then(|chunks| consume_stream(chunks, keep_chunks, &mut on_delta));
                self.finish_call(dispatch, outcome)
            }
        }
    }

    /// Pull chunks from an async producer. `None` ends the stream. The closure
    /// can own a network stream without requiring a particular executor crate.
    pub async fn amodel_stream<F, Fut, O, OFut>(
        &mut self,
        payload: Value,
        options: CallOptions,
        keep_chunks: bool,
        next_chunk: F,
        on_delta: O,
    ) -> Result<Node>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<Option<Value>>>,
        O: FnMut(Value) -> OFut,
        OFut: Future<Output = Result<()>>,
    {
        self.async_stream_call(
            NodeKind::ModelCall,
            payload,
            options,
            keep_chunks,
            next_chunk,
            on_delta,
        )
        .await
    }

    pub async fn atool_stream<F, Fut, O, OFut>(
        &mut self,
        name: &str,
        args: Value,
        options: CallOptions,
        keep_chunks: bool,
        next_chunk: F,
        on_delta: O,
    ) -> Result<Node>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<Option<Value>>>,
        O: FnMut(Value) -> OFut,
        OFut: Future<Output = Result<()>>,
    {
        if !args.is_object() {
            return Err(crate::Error::Invalid("tool args must be object".into()));
        }
        self.async_stream_call(
            NodeKind::ToolCall,
            json!({"tool":name,"args":args}),
            options,
            keep_chunks,
            next_chunk,
            on_delta,
        )
        .await
    }

    async fn async_stream_call<F, Fut, O, OFut>(
        &mut self,
        kind: NodeKind,
        payload: Value,
        options: CallOptions,
        keep_chunks: bool,
        next_chunk: F,
        mut on_delta: O,
    ) -> Result<Node>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<Option<Value>>>,
        O: FnMut(Value) -> OFut,
        OFut: Future<Output = Result<()>>,
    {
        let _guard = self.operation_lock()?;
        if kind == NodeKind::ToolCall {
            self.ensure_unfenced_tool()?;
        }
        match self.prepare_call(kind, payload, options)? {
            CallPreparation::Recorded(node) => {
                if let Some(chunks) = node
                    .result
                    .as_ref()
                    .and_then(|r| r.get("chunks"))
                    .and_then(Value::as_array)
                {
                    for chunk in chunks.iter().filter(|c| c.is_object()) {
                        on_delta(chunk.clone()).await?;
                    }
                }
                Ok(node)
            }
            CallPreparation::Dispatch(dispatch) => {
                let outcome =
                    crate::consume_stream_async(next_chunk, keep_chunks, &mut on_delta).await;
                self.finish_call(dispatch, outcome)
            }
        }
    }
}
