//! Streaming accumulation using the Python 1.6.0 callback result contract.
use crate::{json, Error, Result, Value};

/// Incremental accumulator. Unretained chunks are released immediately.
pub struct StreamAccumulator {
    result: Value,
    chunks: Option<Vec<Value>>,
    received: usize,
}
impl StreamAccumulator {
    pub fn new(keep_chunks: bool) -> Self {
        Self {
            result: json!({}),
            chunks: keep_chunks.then(Vec::new),
            received: 0,
        }
    }
    pub fn push(&mut self, chunk: Value) -> Result<()> {
        let object = chunk
            .as_object()
            .ok_or_else(|| Error::Handler("stream chunks must be JSON objects".into()))?;
        self.received += 1;
        if let Some(result) = object.get("result").filter(|v| !v.is_null()) {
            if !result.is_object() {
                return Err(Error::Handler(
                    "a stream chunk result must be an object".into(),
                ));
            }
            self.result = result.clone();
        } else if let Some(delta) = object.get("delta").filter(|v| !v.is_null()) {
            if !delta.is_object() {
                return Err(Error::Handler(
                    "a stream chunk delta must be an object".into(),
                ));
            }
            merge_value(&mut self.result, delta);
        } else {
            merge_value(&mut self.result, &chunk);
        }
        if let Some(chunks) = &mut self.chunks {
            chunks.push(chunk);
        }
        Ok(())
    }
    pub fn received(&self) -> usize {
        self.received
    }
    pub fn finish(mut self) -> Value {
        if let Some(chunks) = self.chunks {
            self.result["chunks"] = json!(chunks);
        }
        self.result
    }
}

fn merge_value(target: &mut Value, delta: &Value) {
    match (target, delta) {
        (Value::Object(target), Value::Object(delta)) => {
            for (key, value) in delta {
                match target.get_mut(key) {
                    Some(current) => merge_value(current, value),
                    None => {
                        target.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        (Value::String(current), Value::String(value)) => current.push_str(value),
        (Value::Array(current), Value::Array(value)) => current.extend(value.iter().cloned()),
        (current, value) => *current = value.clone(),
    }
}

/// Consume fallible chunks, invoking the observer before merging each snapshot.
pub fn consume_stream<I, F>(chunks: I, keep_chunks: bool, mut on_delta: F) -> Result<Value>
where
    I: IntoIterator<Item = Result<Value>>,
    F: FnMut(&Value) -> Result<()>,
{
    let mut accumulator = StreamAccumulator::new(keep_chunks);
    for chunk in chunks {
        let chunk = chunk.map_err(|e| {
            if accumulator.received() > 0 {
                outcome_unknown(e)
            } else {
                e
            }
        })?;
        if !chunk.is_object() {
            return Err(outcome_unknown(Error::Handler(
                "stream chunks must be JSON objects".into(),
            )));
        }
        on_delta(&chunk).map_err(outcome_unknown)?;
        accumulator.push(chunk).map_err(outcome_unknown)?;
    }
    Ok(accumulator.finish())
}

/// Consume an asynchronous pull stream without imposing an executor or retaining
/// discarded chunks. Observer and producer failures after output are uncertain.
pub async fn consume_stream_async<F, Fut, O, OFut>(
    mut next_chunk: F,
    keep_chunks: bool,
    mut on_delta: O,
) -> Result<Value>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<Option<Value>>>,
    O: FnMut(Value) -> OFut,
    OFut: std::future::Future<Output = Result<()>>,
{
    let mut accumulator = StreamAccumulator::new(keep_chunks);
    loop {
        let chunk = next_chunk().await.map_err(|error| {
            if accumulator.received() > 0 {
                outcome_unknown(error)
            } else {
                error
            }
        })?;
        let Some(chunk) = chunk else { break };
        if !chunk.is_object() {
            return Err(outcome_unknown(Error::Handler(
                "stream chunks must be JSON objects".into(),
            )));
        }
        on_delta(chunk.clone()).await.map_err(outcome_unknown)?;
        accumulator.push(chunk).map_err(outcome_unknown)?;
    }
    Ok(accumulator.finish())
}

pub(crate) fn outcome_unknown(error: Error) -> Error {
    match error {
        Error::OutcomeUnknown(_) => error,
        other => Error::OutcomeUnknown(Box::new(other)),
    }
}

/// Reemit retained chunks on replay without contacting the original provider.
pub fn reemit_chunks<F>(result: &Value, mut on_delta: F) -> Result<()>
where
    F: FnMut(&Value) -> Result<()>,
{
    if let Some(chunks) = result.get("chunks").and_then(Value::as_array) {
        for chunk in chunks.iter().filter(|v| v.is_object()) {
            on_delta(chunk)?;
        }
    }
    Ok(())
}
