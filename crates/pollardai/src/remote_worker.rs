//! Isolates database runtimes from callers' Tokio runtimes and callback threads.
use crate::{Error, Result};
use std::sync::mpsc::{self, Sender};

type Job<T> = Box<dyn FnOnce(&mut T) + Send>;
pub(crate) struct Worker<T> {
    sender: Sender<Job<T>>,
}
impl<T: 'static> Worker<T> {
    pub(crate) fn new(init: impl FnOnce() -> Result<T> + Send + 'static) -> Result<Self> {
        let (sender, receiver) = mpsc::channel::<Job<T>>();
        let (ready, result) = mpsc::sync_channel(1);
        std::thread::Builder::new()
            .name("pollard-database".into())
            .spawn(move || {
                let mut state = match init() {
                    Ok(state) => {
                        let _ = ready.send(Ok(()));
                        state
                    }
                    Err(error) => {
                        let _ = ready.send(Err(error));
                        return;
                    }
                };
                for job in receiver {
                    job(&mut state);
                }
            })
            .map_err(|e| Error::Invalid(format!("cannot start database worker: {e}")))?;
        result.recv().map_err(|_| disconnected())??;
        Ok(Self { sender })
    }
    pub(crate) fn call<R: Send + 'static>(
        &self,
        op: impl FnOnce(&mut T) -> Result<R> + Send + 'static,
    ) -> Result<R> {
        let (sender, receiver) = mpsc::sync_channel(1);
        self.sender
            .send(Box::new(move |state| {
                let _ = sender.send(op(state));
            }))
            .map_err(|_| disconnected())?;
        receiver.recv().map_err(|_| disconnected())?
    }
}
fn disconnected() -> Error {
    Error::Backend {
        detail: "database worker disconnected".into(),
        connection_lost: true,
    }
}
