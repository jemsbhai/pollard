# Govern teams of agents

Pollard 1.6.1 gives each agent a recorded identity, an independent execution
cursor, inherited limits, and an optional list of permitted tools. A team can
share a budget while its agents work in parallel. Agents can pass references
to recorded results, move an explicit context between workers, and restore a
saved cursor. Reports attribute work to agents and check recorded dependencies.

These team APIs are Python 1.6.1 additions. The independently versioned npm and
Rust packages target Python 1.6.0 behavior and do not yet expose these team APIs.

Your application still decides which agents exist, what they do, when they
run, and how to judge their output. Use Pollard around the model and tool calls
made by that application. Calls that bypass Pollard do not enter its ledger or
consume its budgets.

## Run the complete examples

From the repository root:

```powershell
python -m pip install -e .
python examples\15_agent_team.py
python examples\16_distributed_team.py
python examples\17_team_comparison.py
python examples\18_durable_approval.py
```

All four use standard Python and Pollard. They make no network requests, need
no credentials, and incur no hosted-model charges. Their model-shaped functions
return fixed data with declared synthetic token counts. Example 16 creates a
temporary SQLite database and removes it on exit. Example 18 creates a temporary
database and JSON documents, then removes them. Examples 15 and 17 use memory.

| Example | Workflow | Result to inspect |
|---|---|---|
| [15: Agent team](https://github.com/jemsbhai/pollard/blob/main/examples/15_agent_team.py) | Planner, three delegated specialists, reviewer, handoff, tool denial, and strict replay | Eight executed calls, zero publish calls, matching replay result, and zero replay dispatches |
| [16: Distributed team](https://github.com/jemsbhai/pollard/blob/main/examples/16_distributed_team.py) | Four independent SQLite connections attach serialized contexts and contend for three steps | Three accepted calls, one refusal, four distinct starting cursors, and restored checkpoints |
| [17: Team comparison](https://github.com/jemsbhai/pollard/blob/main/examples/17_team_comparison.py) | One worker and three workers process the same fixed records | Equal task correctness, one versus three calls, 23 versus 33 synthetic tokens, and measured local elapsed time |
| [18: Durable approval](https://github.com/jemsbhai/pollard/blob/main/examples/18_durable_approval.py) | Store an approval request, reconstruct the worker, and execute the approved action | No dispatch before approval, one handler call after restoration, and one dummy receipt |

## Create a team and give each task a cursor

This complete program records a two-agent workflow. Save it as `team_start.py`
and run `python team_start.py` after installing Pollard:

```python
from pollard import Budget, MemoryStore, Runtime, Team
from pollard.dependencies import ResultReference, record_dependency
from pollard.team_reports import team_report


def local_model(payload):
    return {
        "text": payload["input"],
        "usage": {"input_tokens": 2, "output_tokens": 2},
    }


store = MemoryStore()
team = Team(Runtime(store), "first-team", budget=Budget(steps=2))
researcher = team.agent("researcher", task_id="collect", role="research")
reviewer = team.agent("reviewer", task_id="check", role="review")

finding = researcher.run.model_call(
    {"model": "local-demo", "input": "The measured count is 12."},
    fn=local_model,
)
record_dependency(
    reviewer.run,
    [ResultReference.from_node(finding)],
    label="evidence for review",
)
answer = reviewer.run.model_call(
    {"model": "local-demo", "input": "Checked: " + finding.result["text"]},
    fn=local_model,
)
print(answer.result["text"])
print(team_report(store, team.root_id).to_dict())
```

`Team` records a manifest under its run root. Each `agent()` call creates a
recorded assignment and a separate `Run`. Model and tool calls extend that
agent's cursor, so simultaneous workers do not compete to update one Python
cursor. `agent_id` identifies an actor; `task_id` identifies its assignment;
`role` is an optional application label. Supply stable, non-secret values.

Reuse the same labels and configuration when replaying a recording. Give a new
live job a distinct team label or attempt when it represents a new execution.
An actor that receives a different assignment should receive a different
`task_id`. Do not share one mutable `TeamAgent.run` among simultaneous tasks.

`agent.run.agent_identity` exposes the current identity. Tool policies receive
the same value in `PolicyContext.agent_identity`. Labels are application claims,
not authenticated user identities. Authenticate workers and authorize task
assignment in the application that creates or accepts contexts.

## Nest work under a budget

A team budget limits the whole team. An agent budget adds a limit for that
assignment and its delegated descendants. A child consumes every inherited
scope as well as its own scope. A smaller child limit cannot increase an
ancestor's remaining capacity.

```python
from pollard import Budget, MemoryStore, Runtime, Team

team = Team(Runtime(MemoryStore()), "nested-limits", budget=Budget(steps=20))
planner = team.agent("planner", task_id="plan", budget=Budget(steps=10))
researcher = planner.delegate(
    "researcher", task_id="research", budget=Budget(steps=4)
)
reviewer = team.agent("reviewer", task_id="review", budget=Budget(steps=3))
```

The researcher can use at most four steps, subject to the planner's ten-step
limit and the team's twenty-step limit. The reviewer shares the team limit
but does not consume the planner's scope. A budget is a ceiling, not a reserved
allocation: another admitted worker can consume shared capacity first. Pollard
does not assign priority, queue denied work, or guarantee each worker a turn.

Step reservations are exact: an admitted model or tool call consumes one step.
For concurrent enforcement, every worker must use the same transactional
store and logical namespace. SQLite supports separate processes sharing a local
database. PostgreSQL, Redis, MongoDB, and Neo4j provide the corresponding shared
store contract for workers on different hosts. Kafka is an audit store and does
not provide the transactional arbitration required for a shared exact cap.

Tokens and dollars depend on the configured meters and provider usage. A token
estimate is a reservation estimate; final usage can exceed it after dispatch.
Elapsed-call charges are measured after execution. A seconds budget does not
interrupt a running Python function or cancel a provider request. For meter
details, see the
[API reference](https://github.com/jemsbhai/pollard/blob/main/docs/api-reference.md)
and [scale-out guide](https://github.com/jemsbhai/pollard/blob/main/docs/scale-out.md).

## Share a provider allowance across independent teams

Separate team roots normally have separate run budgets. A named `SharedBudget`
adds a cumulative limit across roots that use the same store and scope name.
A named `WindowMeter` adds a sliding-window limit across those roots. This
complete program permits two calls across two teams, then refuses a third:

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from pollard import (
    Budget, BudgetExceeded, Runtime, SharedBudget, SQLiteStore, Team, WindowMeter,
)
from pollard.meters import StepMeter


def local_model(payload):
    return {"text": payload["input"]}


with TemporaryDirectory() as directory:
    with SQLiteStore(Path(directory) / "provider.db") as store:
        runtime = Runtime(
            store,
            meters=[StepMeter(), WindowMeter("requests", 10, 60, scope="demo-provider")],
            shared_budgets=[SharedBudget("demo-provider", Budget(steps=2))],
        )
        first = Team(runtime, "customer-a").agent("worker", task_id="first")
        second = Team(runtime, "customer-b").agent("worker", task_id="second")
        for agent in (first, second):
            agent.run.model_call({"input": "hello"}, fn=local_model)
        try:
            second.run.model_call({"input": "one more"}, fn=local_model)
        except BudgetExceeded:
            print("The shared two-step allowance is exhausted.")
```

All participants must use identical configuration for the same named scope.
Changing a limit, window length, or underlying window-meter class is rejected
instead of creating an independent allowance under the same name. To start a
new cumulative allowance or change its configuration, choose a new scope name,
such as one containing an application-managed period. Named scopes require a
transactional store.

The default meter beneath `WindowMeter("requests", ...)` counts both governed
model calls and governed tool calls. Scope provider-only runtimes appropriately,
or supply a meter that counts the intended operation. A local allowance governs
the calls routed through these runtimes; it does not read or change an external
provider's account quota.

## Restrict the tools an assignment can use

Pass `allowed_tools` to the team or an individual assignment. A delegated
assignment may keep or narrow its inherited list, but cannot widen it. An empty
tuple permits no tools. An omitted value inherits the parent restriction.
The team ceiling also applies to the coordinator's `team.run` and its branches.
Restricted assignments require a registry so action names resolve to known
specifications before execution.

```python
team = Team(runtime, "research", allowed_tools=("search", "read_document"))
researcher = team.agent("researcher", task_id="collect")
reviewer = team.agent("reviewer", task_id="review", allowed_tools=())
```

In this fragment, `runtime` must have a registry containing the named tools.
The complete registry setup is in example 15. That example registers both a
read action and a publish action, then proves that the restricted reviewer
cannot invoke publish. The handler is a sentinel that fails if reached.

Tool permissions supplement the registry schema and policy checks. They govern
tool dispatch through Pollard. They do not restrict what arbitrary Python code
can do outside the runtime. Bind model-selected tool names and arguments through
the agent's `run.tool_call()` or `run.atool_call()` boundary.

For actions requiring human approval, use a policy returning `Decision.CONFIRM`.
The application catches `ConfirmationRequired`, presents the action to its
authorized reviewer, and calls `confirm()` only after approval. An agent's role
label does not itself grant human approval. The
[confirmation example](https://github.com/jemsbhai/pollard/blob/main/examples/12_dry_run_confirmation.py)
shows the in-memory confirmation lifecycle. Use the durable approval protocol
below when a review can outlive its worker process.

## Persist an approval across worker restarts

`ApprovalPolicy` reads a stored decision for the exact next action. An approval
request binds the run, current agent identity, registry, tool name and version,
tool specification, and a digest of its redacted arguments. The policy normally
applies to tools marked `side_effects=True`; `side_effects_only=False` also
requires approval for registered read actions. Missing, mismatched, denied, or
exhausted approvals are denied. Start the review explicitly with
`request_approval()`; an in-memory `confirm()` token cannot grant durable approval.

Construct each worker runtime with the same registry and a policy connected to
that worker's store:

```python
from pollard import Budget, Runtime, Team
from pollard.approvals import ApprovalPolicy


def make_team(store, registry):
    runtime = Runtime(store, registry=registry, policies=[ApprovalPolicy(store)])
    return Team(runtime, "delivery-team", budget=Budget(steps=1), allowed_tools=("enqueue",))
```

The full runnable
[approval example](https://github.com/jemsbhai/pollard/blob/main/examples/18_durable_approval.py)
provides the registry, dummy action, temporary files, and every connection
lifecycle. The worker protocol is:

1. Create the assignment and call `request_approval(agent.run, "enqueue", args)`.
   This writes a request note and advances the cursor without dispatching the
   action. Keep the exact arguments in the application's protected task state.
2. Persist `request.to_dict()` and `agent.checkpoint().to_dict()`. A tool call
   without an approved decision raises `PolicyViolation` before its handler
   runs. The worker may now stop and close its connection.
3. An application reviewer service authenticates the reviewer and presents the
   exact intended action. After the reviewer decides, load the request with
   `ApprovalRequest.from_dict()` and call
   `decide_approval(store, request, approved=True, reviewer="operator-id")`.
   Use `approved=False` to record a denial.
4. Recreate the worker's store, runtime, and team. Restore its saved checkpoint,
   then make the same `tool_call()` with the same arguments. The policy reads
   the persistent decision and allows or denies that exact action.

`decide_approval()` is an explicit write operation for the trusted reviewer
service. It does not authenticate a human, open a review UI, or establish that
the named reviewer has permission. Repeating the exact decision is idempotent;
a conflicting decision or reviewer is rejected. Keep access to this helper and
its store credentials separate from untrusted agent-generated input.

An approval is sequentially scoped. Any intervening recorded model or tool
call, or a recorded post-dispatch failure, consumes the pending approval's
position. Ordinary notes and checkpoints can be recorded while waiting. A
different action, argument digest, registry, or actor needs a new approval.
The request stores the argument digest rather than the original argument
values, so the application must retain the proposal needed for its review UI
and later execution.

Concurrent workers can still race an approval check. Assign one active owner
to the task and pass a stable operation ID to an external service that enforces
idempotency. The request ID can serve as such a key when the provider accepts
an out-of-band idempotency key; example 18 uses an application operation ID in
the approved arguments. Do not generate a new operation ID merely because a
worker restarted. Reconcile unknown external outcomes before making a retry.

Example 18 rebuilds the Pollard worker and connections while an in-memory dummy
service retains its receipt table. This demonstrates durable approval and
restoration without a real side effect. A production service must persist its
own idempotency records across its own restarts.

## Record dependencies and handoffs

The execution tree records ownership and ancestry. A reviewer can consume
results from several sibling branches, so ancestry alone cannot describe its
inputs. `ResultReference` identifies a stored result and its digest.
`record_dependency()` writes a note describing which results an agent consumes.
`record_handoff()` writes a handoff note with a recipient and optional task ID.

```python
from pollard.dependencies import (
    ResultReference, record_dependency, record_handoff, verify_dependencies,
)

references = [ResultReference.from_node(node) for node in specialist_results]
record_handoff(planner.run, references, recipient="reviewer", task_id="review")
record_dependency(reviewer.run, references, label="review inputs")
diagnostic = verify_dependencies(team.run.store, team.root_id)
assert diagnostic.ok, diagnostic.to_dict()
```

Here `specialist_results` contains completed model or tool nodes from the same
team root. References retain identity without copying a large result into every
handoff note. The receiving application retrieves the result and decides what
content to put in its next model payload. Recording a handoff does not send a
message, start a worker, transfer an external object, or prove the recipient
read it. A dependency records a declared relationship; output quality still
requires an application check.

The verifier checks stored references against their targets. It reports missing
or changed targets through structured findings. Keep referenced results for as
long as dependent records must remain verifiable. Treat any extra retention or
external artifact storage as part of the application's data policy. See
[data governance](https://github.com/jemsbhai/pollard/blob/main/docs/data-governance.md).

## Move an assignment between workers

`agent.context()` captures the exact cursor and inherited scope definitions.
Serialize its `to_dict()` result as JSON and pass that document through your
queue or worker transport. The receiving worker opens its own connection to
the same store, recreates the team with the same configuration, and calls
`team.attach(context_dict)`.

```python
import json

transport_document = json.dumps(agent.context().to_dict())

# On a worker that has opened the same store and recreated the same Team:
attached = worker_team.attach(json.loads(transport_document))
result = attached.run.model_call(payload, fn=model_client)
```

`attach()` validates the context against immutable records in the store. It
does not choose the deepest leaf in the entire team tree. That matters when
several branches are active: the deepest leaf may belong to a different agent.
Do not use a generic deepest-leaf resume as a replacement for an agent context.

The team binds the registry, meter, policy, dry-run, and budget configuration.
Recreate that configuration on each worker before attaching a context. Custom
meters, estimators, and policies must expose `pollard_team_config()` to return
stable JSON configuration for this comparison. Include all settings
that affect governance and exclude credentials, open connections, and mutable
counters. A declared configuration is a contract between application workers;
it does not verify their source code or authenticate a remote process.

Context transport is not a task queue or an exclusive worker claim. Deliver an
assignment to one active owner at a time, or use application-level claim leases.
Reattaching the same context to two workers creates two cursors at the same
point; it does not guarantee only one external action can run. Distinct task IDs
separate intentional parallel assignments.

Example 16 includes the full coordinator and worker code, JSON roundtrip,
separate SQLite connections, a simultaneous start, refusal handling, and
checkpoint restoration. It uses threads to keep the demonstration small while
exercising independent connections. To use processes, pass only the database
path and JSON context into a top-level worker function, open the store inside
that function, and protect process startup with `if __name__ == "__main__":`.
Do not pickle an open connection, `Runtime`, or mutable `Run` between workers.

For workers on different hosts, replace SQLite with the same configured
`PostgresStore`, `RedisStore`, `MongoStore`, or `Neo4jStore` namespace in every
process. Credentials stay in each worker's connection configuration, outside
the context. The
[distributed-store example](https://github.com/jemsbhai/pollard/blob/main/examples/09_distributed_stores.py)
and [operations guide](https://github.com/jemsbhai/pollard/blob/main/docs/distributed-stores.md)
provide complete backend setup and connection lifecycles.

## Run workers in separate processes

Save this complete program as `team_processes.py` and run it as a file. Each
process opens its own SQLite connection and attaches its own JSON context. The
two-step team limit is shared through the database:

```python
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from pollard import Budget, Runtime, SQLiteStore, Team


def local_model(payload):
    return {
        "text": payload["input"],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def execute(job):
    database_path, context_json = job
    with SQLiteStore(database_path) as store:
        team = Team(Runtime(store), "process-team", budget=Budget(steps=2))
        agent = team.attach(json.loads(context_json))
        node = agent.run.model_call(
            {"input": agent.identity.task_id}, fn=local_model
        )
        return node.result["text"]


def main():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "team.db"
        with SQLiteStore(path) as store:
            team = Team(Runtime(store), "process-team", budget=Budget(steps=2))
            jobs = [
                (str(path), json.dumps(team.agent(
                    f"worker-{index}", task_id=f"task-{index}"
                ).context().to_dict()))
                for index in range(2)
            ]
        with ProcessPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(execute, jobs))
        assert results == ["task-0", "task-1"]
        print(results)


if __name__ == "__main__":
    main()
```

For a host-to-host deployment, a remote worker follows the same sequence: open
the configured transactional store, recreate the team, attach the received
context, and make its governed calls. Use a remote store with the same
`store_id` on every host. For example, this connection factory replaces each
`SQLiteStore(...)` call when PostgreSQL is configured:

```python
import os

from pollard import PostgresStore


def open_team_store():
    return PostgresStore(os.environ["POLLARD_PG_DSN"], store_id="team-workers")
```

Install `pollard[pg]` on each worker and configure `POLLARD_PG_DSN` through its
deployment environment. The transport carries the context JSON, not the DSN.
Keep task creation separate from worker dispatch so retries do not silently
create a new assignment. A queue's visibility timeout and delivery guarantees
must be coordinated with the application's task ownership and retry policy.

## Save an exact checkpoint and recover deliberately

After a completed step, persist `agent.checkpoint().to_dict()` with the
application's task state. A replacement worker recreates the same team and
calls `team.restore(checkpoint_dict)`. Restoration validates the checkpoint and
returns the recorded assignment at its saved cursor. The example below uses
the already-created `team` and `agent`:

```python
import json

checkpoint_document = json.dumps(agent.checkpoint().to_dict())
restored = team.restore(json.loads(checkpoint_document))
assert restored.run.cursor_id == agent.run.cursor_id
```

A Pollard checkpoint captures ledger position and governance context. Save
application state separately, including task status, pending work, and the
references needed to reconstruct model inputs. It does not capture a Python
stack, open socket, framework scheduler, or in-flight model request. The original
`confirm()` tokens belong to the active run in memory. Durable approval requests
and decisions are stored separately and can be read by a replacement worker
using the protocol above.

An external side effect and a ledger write are separate operations. If the
external action succeeded but its result was not committed, replay has no
completed result to return. If an adapter reports an unknown outcome or
settlement uncertainty, reconcile with the external service before retrying.
Use a provider-supported idempotency key derived from a stable application
operation ID, and retain that key across recovery. Pollard's budget reservation
does not make an external API call exactly once.

Record completed handoff delivery or receipt in application state when it
matters. A checkpoint and a handoff note do not by themselves provide durable
message delivery, a retry policy, or cancellation propagation. These remain
responsibilities of the worker framework and transport.

## Replay a team's completed calls

Open the same store in `Runtime(mode="replay")`, construct the same team and
assignments, and run the same call and dependency sequence. Completed model and
tool results come from the recording. A missing recording raises an error
instead of dispatching a live call. Example 15 supplies replacement model and
tool functions that fail if invoked, then compares the final result IDs.
Its exhausted-budget probe runs only during recording: a refused model call
does not have a completed model result for strict replay to return.

Replay repeats the application's traversal of a recording. It does not rerun
the original thread schedule or prove that a live provider would return the
same output now. Use
[live revalidation](https://github.com/jemsbhai/pollard/blob/main/docs/revalidation.md)
for an explicit fresh observation, with its own budget and result record.

## Use async execution or an existing framework

`Team` also accepts `AsyncRuntime`. Each assignment exposes an `AsyncRun` and
can await its own calls. This complete program runs two local async callables:

```python
import asyncio

from pollard import AsyncRuntime, Budget, MemoryStore, Team


async def local_model(payload):
    await asyncio.sleep(0)
    return {
        "text": payload["input"],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


async def main():
    team = Team(AsyncRuntime(MemoryStore()), "async-team", budget=Budget(steps=2))
    agents = [team.agent(f"worker-{index}", task_id=f"task-{index}") for index in range(2)]
    nodes = await asyncio.gather(*[
        agent.run.amodel_call({"input": agent.identity.task_id}, fn=local_model)
        for agent in agents
    ])
    print([node.result["text"] for node in nodes])


asyncio.run(main())
```

The framework can continue to own routing, parallel scheduling, retries, and
state transitions. Associate each framework assignment with a Pollard agent,
then wrap each actual model request and registered tool action. A whole
framework invocation wrapped as one model call provides only one outer step;
it cannot separately govern or attribute hidden internal requests. Pass the
appropriate `TeamAgent` or serialized context into each graph node or worker.

Start from the
[integration recipes](https://github.com/jemsbhai/pollard/blob/main/docs/recipes/README.md)
for provider adapters, LangGraph nodes, LangChain workflows, and typed agent
integrations. Model clients and their transport configuration remain outside
Pollard. Decide explicitly whether a framework retry is a new attempt and what
idempotency key it will use for any external effect.

## Inspect without changing the recording

`team_report(store, root_id)` and `verify_dependencies(store, root_id)` read the
stored team. For SQLite, open a read-only connection when investigating a saved
recording:

```python
from pollard import SQLiteStore
from pollard.dependencies import verify_dependencies
from pollard.team_reports import team_report

root_id = "replace-with-the-recorded-team-root-id"
with SQLiteStore("team.db", read_only=True) as store:
    print(team_report(store, root_id).to_dict())
    print(verify_dependencies(store, root_id).to_dict())
```

The report separates team totals, attributed agents, and unattributed calls.
It includes model calls, tool calls, refusals, recorded charges and usage,
dependency notes, handoffs, and summed call durations. Use the unattributed
group to find calls made outside an agent assignment. Inspect failed dependency
findings before trusting a result assembled from several branches.

Each call is attributed once to its nearest agent assignment. A parent's report
row does not count its children's charges again, although inherited budgets
still charge those children against the parent's limit. Accounting uses recorded
metadata, which is mutable and is not protected by a subtree seal. Validate the
recording and retain any external accounting evidence required by your use case.

Summed call duration is not team elapsed time. Parallel calls overlap, and
queueing, orchestration, idle workers, retries, and final synthesis can add time
outside a model or tool call. Measure end-to-end elapsed time in the application
when comparing workflows. Reports describe recorded relationships and usage;
they do not infer a critical path, causal proof, idle time, task correctness,
or whether another agent was worth its cost.

## Compare team value against a single agent

Use the same inputs and a declared outcome check for both configurations. Count
the planner, routing, communication, verification, and retry calls when they
exist. Record token usage and configured price assumptions as well as elapsed
time. Compare several representative tasks before changing a production
workflow. More agents can add useful independent work, but they also add calls,
context transfer, waiting, and failure paths.

Example 17 makes the comparison mechanics inspectable. Both configurations
select the same three marked rows from six fixed records. The one-worker path
uses one call and 23 synthetic tokens; the three-worker path uses three calls
and 33 synthetic tokens because each call carries a fixed overhead. Both
achieve the same declared exact-match score. The measured duration describes
that local Python invocation, including setup and reporting. It is not an LLM
benchmark, and the example asserts no timing advantage.

For a real task, replace the selector with the governed provider adapter,
record actual normalized usage, define an outcome rubric before running, and
retain both configurations' results. Pollard supplies the accounting and
inspectable execution evidence. The application supplies the task's value
judgment and chooses whether the measured tradeoff warrants a team.
