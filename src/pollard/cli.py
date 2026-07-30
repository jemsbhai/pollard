"""Command-line inspection for Pollard recordings and stores."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any
from unicodedata import category
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from .errors import PollardError
from .governance import export_subtree, gc, import_subtree
from .governor import charge_to_decimal, charge_to_json, recompute_charges
from .merge import _merge_prepared, _MergeSpool
from .redaction import contains_redaction
from .seal import seal
from .store import Store
from .stores import (
    KafkaStore,
    MongoStore,
    Neo4jStore,
    PostgresStore,
    RedisStore,
    SQLiteStore,
)
from .tree import Node, NodeKind
from .verify import verify

_MONGO_COLLECTION_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_NEO4J_URI_SCHEMES = frozenset(
    {"neo4j", "neo4j+s", "neo4j+ssc", "bolt", "bolt+s", "bolt+ssc"}
)
_NEO4J_DIRECT_PREFIXES = (
    "neo4j://",
    "neo4j+s://",
    "neo4j+ssc://",
    "bolt://",
    "bolt+s://",
    "bolt+ssc://",
)
_REMOTE_STORE_PREFIXES = (
    "pg-env:",
    "postgres://",
    "postgresql://",
    "redis-env:",
    "redis://",
    "rediss://",
    "mongo-env:",
    "mongodb://",
    "mongodb+srv://",
    "neo4j-env:",
    *_NEO4J_DIRECT_PREFIXES,
    "kafka-env:",
    "kafka://",
)


def _parse_query_parameters(query: str) -> list[tuple[str, str]]:
    """Parse a non-empty store query consistently across supported Python versions."""
    if not query:
        return []
    return parse_qsl(
        query,
        keep_blank_values=True,
        strict_parsing=True,
        errors="strict",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ImportError, KeyError, OSError, PollardError, TypeError, ValueError) as exc:
        print(f"pollard: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pollard", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    store_help = (
        "SQLite path, PostgreSQL store spec, Redis store spec, "
        "MongoDB store spec, Neo4j store spec, or Kafka store spec"
    )

    show = subparsers.add_parser("show", help="render a stored run tree")
    show.add_argument("db", help=store_help)
    show.add_argument("root_id")
    show.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    show.add_argument("--unicode", action="store_true", help="use Unicode tree connectors")
    show.add_argument(
        "--payloads",
        action="store_true",
        help="include payloads and results; they may contain sensitive content",
    )
    show.add_argument("--html", type=Path, help="write a self-contained HTML tree")
    show.set_defaults(handler=_show)

    report = subparsers.add_parser("report", help="summarize spent and avoided charges")
    report.add_argument("db", help=store_help)
    report.add_argument("root_id")
    report.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    report.set_defaults(handler=_report)

    check = subparsers.add_parser("verify", help="verify stored identities and results")
    check.add_argument("db", help=store_help)
    check.add_argument("root_id", nargs="?")
    check.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    check.set_defaults(handler=_verify)

    seal_parser = subparsers.add_parser("seal", help="create a subtree seal report")
    seal_parser.add_argument("db", help=store_help)
    seal_parser.add_argument("root_id")
    seal_parser.add_argument("--output", type=Path, help="write the full JSON report")
    seal_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    seal_parser.set_defaults(handler=_seal)

    runs = subparsers.add_parser("runs", help="list run roots in a store")
    runs.add_argument("stores", nargs="+", help=store_help)
    runs.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    runs.set_defaults(handler=_runs)

    gc_parser = subparsers.add_parser("gc", help="run explicit offline maintenance")
    gc_parser.add_argument("db", help="SQLite path")
    gc_parser.add_argument(
        "mode",
        choices=("drop-pruned", "compact"),
        nargs="?",
        default="drop-pruned",
    )
    gc_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    gc_parser.set_defaults(handler=_gc)

    export_parser = subparsers.add_parser("export", help="export a sealed subtree")
    export_parser.add_argument("db", help=store_help)
    export_parser.add_argument("root_id")
    export_parser.add_argument("path", type=Path)
    export_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    export_parser.set_defaults(handler=_export)

    import_parser = subparsers.add_parser("import", help="import a sealed subtree")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("db", help="destination SQLite path")
    import_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    import_parser.set_defaults(handler=_import)

    merge_parser = subparsers.add_parser("merge", help="merge one or more stores")
    merge_parser.add_argument(
        "destination",
        help=(
            "destination SQLite path, PostgreSQL store spec, "
            "redis-env:, mongo-env:, neo4j-env:, or kafka-env: store spec"
        ),
    )
    merge_parser.add_argument("sources", nargs="+", help=store_help)
    merge_parser.add_argument(
        "--replay",
        action="store_true",
        help="reject result conflicts instead of recording them",
    )
    merge_parser.add_argument(
        "--initialize-if-missing",
        action="store_true",
        help=(
            "initialize a missing redis-env:, mongo-env:, or neo4j-env: destination "
            "namespace; remote destinations are existing-only by default"
        ),
    )
    merge_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    merge_parser.set_defaults(handler=_merge)
    return parser


def _show(args: argparse.Namespace) -> int:
    with _open_store(args.db, create=False) as store:
        store.get(args.root_id)
        if args.html is not None:
            document = render_html(store, args.root_id, include_payloads=args.payloads)
            with args.html.open("w", encoding="utf-8", newline="\n") as output:
                output.write(document)
            outcome = {
                "root_id": args.root_id,
                "output": str(args.html),
                "bytes": len(document.encode("utf-8")),
            }
            _emit(outcome if args.json else str(args.html), json_output=args.json)
            return 0
        if args.json:
            _emit(
                tree_document(store, args.root_id, include_payloads=args.payloads),
                json_output=True,
            )
            return 0
        print(
            render_ascii(
                store,
                args.root_id,
                unicode=args.unicode,
                include_payloads=args.payloads,
            )
        )
    return 0


def _report(args: argparse.Namespace) -> int:
    with _open_store(args.db, create=False) as store:
        nodes = list(store.walk(args.root_id))
        document = {
            "root_id": args.root_id,
            "nodes": len(nodes),
            "spent": recompute_charges(store, args.root_id),
            "avoided": _sum_meta_charges(nodes, "avoided"),
        }
    if args.json:
        _emit(document, json_output=True)
        return 0
    print(f"run {args.root_id[:8]} ({document['nodes']} nodes)")
    _print_meter_group("spent", document["spent"])
    _print_meter_group("avoided", document["avoided"])
    return 0


def _verify(args: argparse.Namespace) -> int:
    with _open_store(args.db, create=False) as store:
        roots = [args.root_id] if args.root_id is not None else store.roots()
        findings: dict[tuple[str, str], dict[str, str]] = {}
        node_count = 0
        for root_id in roots:
            for node in store.walk(root_id):
                node_count += 1
                try:
                    report = verify(store, node.id)
                except KeyError as exc:
                    key = (node.id, f"missing ancestor: {exc.args[0]}")
                    findings[key] = {"node_id": key[0], "message": key[1]}
                    continue
                for finding in report.findings:
                    key = (finding.node_id, finding.message)
                    findings[key] = {"node_id": key[0], "message": key[1]}
        ordered = [findings[key] for key in sorted(findings)]
        document = {
            "ok": not ordered,
            "roots": roots,
            "nodes": node_count,
            "findings": ordered,
        }
    if args.json:
        _emit(document, json_output=True)
    elif document["ok"]:
        print(f"OK: {len(roots)} roots, {node_count} nodes")
    else:
        for stored_finding in ordered:
            print(
                f"FAIL {stored_finding['node_id']}: {stored_finding['message']}"
            )
    return 0 if document["ok"] else 1


def _seal(args: argparse.Namespace) -> int:
    with _open_store(args.db, create=False) as store:
        document = seal(store, args.root_id).to_dict()
    if args.output is not None:
        args.output.write_text(_json(document) + "\n", encoding="utf-8")
        outcome = {
            "root_id": document["root_id"],
            "digest": document["digest"],
            "output": str(args.output),
        }
        _emit(outcome if args.json else str(args.output), json_output=args.json)
    else:
        _emit(document if args.json else str(document["digest"]), json_output=args.json)
    return 0


def _runs(args: argparse.Namespace) -> int:
    runs: list[dict[str, Any]] = []
    for spec in args.stores:
        with _open_store(spec, create=False) as store:
            store_label = _store_label(spec)
            for root_id in store.roots():
                root = store.get(root_id)
                nodes = list(store.walk(root_id))
                runs.append(
                    {
                        "store": store_label,
                        "root_id": root_id,
                        "label": _label(root),
                        "attempt": root.attempt,
                        "nodes": len(nodes),
                        "pruned": sum(node.meta.get("pruned") is True for node in nodes),
                    }
                )
    document = {"runs": runs}
    if args.json:
        _emit(document, json_output=True)
        return 0
    if not runs:
        print("no runs")
        return 0
    multiple = len(args.stores) > 1
    for run in runs:
        prefix = f"{run['store']}  " if multiple else ""
        print(
            f"{prefix}{run['root_id'][:8]}  {run['label']}  "
            f"nodes={run['nodes']} pruned={run['pruned']}"
        )
    return 0


def _merge(args: argparse.Namespace) -> int:
    reports: list[dict[str, Any]] = []
    _require_store_destination(
        args.destination,
        initialize_if_missing=args.initialize_if_missing,
    )
    with (
        _prepare_merge_sources(args.sources) as sources,
        _open_store(
            args.destination,
            create=True,
            initialize_if_missing=args.initialize_if_missing,
        ) as destination,
    ):
        for source_spec, plan in sources:
            report = _merge_prepared(destination, plan, replay=args.replay)
            reports.append(
                {"source": _store_label(source_spec), **report.to_dict()}
            )
    document = {
        "destination": _store_label(args.destination),
        "sources": reports,
        "copied": sum(report["copied"] for report in reports),
        "existing": sum(report["existing"] for report in reports),
        "result_conflicts": sum(
            report["result_conflicts"] for report in reports
        ),
        "meta_conflicts": sum(report["meta_conflicts"] for report in reports),
    }
    if args.json:
        _emit(document, json_output=True)
    else:
        print(
            f"{document['destination']}: copied={document['copied']} "
            f"existing={document['existing']} "
            f"result_conflicts={document['result_conflicts']} "
            f"meta_conflicts={document['meta_conflicts']}"
        )
    return 0


@contextmanager
def _prepare_merge_sources(
    specs: Sequence[str],
) -> Iterator[list[tuple[str, _MergeSpool]]]:
    directory = _new_merge_spool_directory()
    primary_error: BaseException | None = None
    try:
        sources: list[tuple[str, _MergeSpool]] = []
        for spec in specs:
            path = _new_merge_spool_path(directory)
            sources.append((spec, _prepare_merge_source(spec, path)))
        for spec, spool in sources:
            try:
                spool.validate()
            except (OSError, PollardError, TypeError, ValueError) as exc:
                raise OSError(
                    f"could not finalize merge source {_store_label(spec)}: {exc}"
                ) from None
        yield sources
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            _cleanup_merge_spool_directory(directory)
        except OSError as cleanup_error:
            if primary_error is None:
                raise OSError(
                    "could not remove private merge preparation data"
                ) from None
            if isinstance(primary_error, Exception):
                raise OSError(
                    f"{primary_error}; private merge preparation cleanup also failed"
                ) from primary_error
            raise cleanup_error


def _prepare_merge_source(spec: str, path: Path) -> _MergeSpool:
    try:
        with _open_store(spec, create=False) as source:
            return _MergeSpool.prepare(path, source)
    except (ImportError, KeyError, OSError, PollardError, TypeError, ValueError) as exc:
        raise OSError(
            f"could not prepare merge source {_store_label(spec)}: {exc}"
        ) from None


def _new_merge_spool_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="pollard-merge-"))
    try:
        path.chmod(0o700)
    except OSError:
        _cleanup_merge_spool_directory(path)
        raise OSError("could not create private merge preparation data") from None
    return path


def _new_merge_spool_path(directory: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix="source-",
            suffix=".sqlite3",
            dir=directory,
        )
        os.close(descriptor)
        path = Path(name)
        path.chmod(0o600)
        return path
    except OSError:
        raise OSError("could not create private merge preparation data") from None


def _cleanup_merge_spool_directory(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError:
        raise OSError("could not remove private merge preparation data") from None


def _gc(args: argparse.Namespace) -> int:
    _require_sqlite_target(args.db, command="gc")
    with _open(Path(args.db)) as store:
        document = gc(store, mode=args.mode).to_dict()
    if args.json:
        _emit(document, json_output=True)
    else:
        print(
            f"{document['mode']}: removed {document['removed_nodes']} nodes "
            f"and {document['removed_blobs']} blobs"
        )
    return 0


def _export(args: argparse.Namespace) -> int:
    with _open_store(args.db, create=False) as store:
        document = export_subtree(store, args.root_id, args.path).to_dict()
    if args.json:
        _emit(document, json_output=True)
    else:
        print(f"{document['path']}  {document['digest']}")
    return 0


def _import(args: argparse.Namespace) -> int:
    _require_sqlite_target(args.db, command="import")
    with SQLiteStore(Path(args.db)) as store:
        document = import_subtree(args.path, store).to_dict()
    if args.json:
        _emit(document, json_output=True)
    else:
        print(
            f"{document['root_id']}  imported={document['imported']} "
            f"existing={document['existing']}"
        )
    return 0


def tree_document(
    store: Store,
    root_id: str,
    *,
    include_payloads: bool = False,
) -> dict[str, Any]:
    nodes = []
    for node in store.walk(root_id):
        item: dict[str, Any] = {
            "id": node.id,
            "parent": node.parent,
            "kind": node.kind,
            "attempt": node.attempt,
            "label": _label(node),
            "charges": _numeric_mapping(node.meta.get("charges")),
            "avoided": _numeric_mapping(node.meta.get("avoided")),
            "refusal": node.kind == NodeKind.REFUSAL.value,
            "pruned": node.meta.get("pruned") is True,
            "redacted": contains_redaction(node.payload),
            "children": store.children(node.id),
        }
        if include_payloads:
            item["payload"] = node.payload
            item["result"] = node.result
        nodes.append(item)
    return {"root_id": root_id, "nodes": nodes}


def render_ascii(
    store: Store,
    root_id: str,
    *,
    unicode: bool = False,
    include_payloads: bool = False,
) -> str:
    tee, elbow, pipe, blank = (
        ("├─ ", "└─ ", "│  ", "   ")
        if unicode
        else ("|-- ", "\\-- ", "|   ", "    ")
    )
    lines: list[str] = []

    def visit(node_id: str, prefix: str, last: bool, root: bool = False) -> None:
        node = store.get(node_id)
        connector = "" if root else (elbow if last else tee)
        lines.append(prefix + connector + _node_summary(node))
        body_prefix = prefix + ("" if root else (blank if last else pipe))
        if include_payloads:
            lines.append(body_prefix + "    payload=" + _compact_json(node.payload))
            if node.result is not None:
                lines.append(body_prefix + "    result=" + _compact_json(node.result))
        children = store.children(node.id)
        for index, child_id in enumerate(children):
            visit(child_id, body_prefix, index == len(children) - 1)

    visit(root_id, "", True, root=True)
    return "\n".join(lines)


def render_html(store: Store, root_id: str, *, include_payloads: bool = False) -> str:
    root = store.get(root_id)
    tree = _html_node(store, root, include_payloads=include_payloads)
    title = escape(f"Pollard run: {_label(root)}")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-monospace, Consolas, monospace; }}
body {{ margin: 2rem; max-width: 100rem; }}
h1 {{ font: 600 1.3rem system-ui, sans-serif; }}
ul {{ list-style: none; margin: 0 0 0 1rem; padding-left: 1rem; border-left: 1px solid #8886; }}
li {{ margin: .35rem 0; }}
summary {{ cursor: pointer; }}
.id {{ color: #777; }}
.charges {{ color: #087f5b; }}
.refusal > details > summary {{ color: #c92a2a; font-weight: 700; }}
.pruned {{ opacity: .5; }}
.redacted > details > summary {{ text-decoration: underline dotted; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; padding: .6rem; background: #8881; }}
</style>
</head>
<body>
<h1>{title}</h1>
<ul>{tree}</ul>
</body>
</html>
"""


def _html_node(store: Store, node: Node, *, include_payloads: bool) -> str:
    classes = []
    if node.kind == NodeKind.REFUSAL.value:
        classes.append("refusal")
    if node.meta.get("pruned") is True:
        classes.append("pruned")
    if contains_redaction(node.payload):
        classes.append("redacted")
    class_attr = f' class="{" ".join(classes)}"' if classes else ""
    charges = _charges_text(node)
    charges_html = f' <span class="charges">{escape(charges)}</span>' if charges else ""
    summary = (
        f"{escape(node.kind)} <span class=\"id\">{node.id[:8]}</span> "
        f"{escape(_label(node))}{charges_html}{_markers(node)}"
    )
    private = ""
    if include_payloads:
        payload = escape(_json(node.payload))
        result = "null" if node.result is None else escape(_json(node.result))
        private = (
            "<details><summary>payload and result</summary>"
            f"<pre>payload={payload}\nresult={result}</pre></details>"
        )
    children = "".join(
        _html_node(store, store.get(child_id), include_payloads=include_payloads)
        for child_id in store.children(node.id)
    )
    nested = f"<ul>{children}</ul>" if children else ""
    return (
        f"<li{class_attr}><details open><summary>{summary}</summary>"
        f"{private}{nested}</details></li>"
    )


def _node_summary(node: Node) -> str:
    charges = _charges_text(node)
    suffix = f" charges[{charges}]" if charges else ""
    markers = ""
    if node.kind == NodeKind.REFUSAL.value:
        markers += " [REFUSED]"
    if node.meta.get("pruned") is True:
        markers += " [PRUNED]"
    if contains_redaction(node.payload):
        markers += " [REDACTED]"
    return f"{node.kind} {node.id[:8]} {_label(node)}{suffix}{markers}"


def _markers(node: Node) -> str:
    markers = []
    if node.kind == NodeKind.REFUSAL.value:
        markers.append("REFUSED")
    if node.meta.get("pruned") is True:
        markers.append("PRUNED")
    if contains_redaction(node.payload):
        markers.append("REDACTED")
    return "" if not markers else " [" + ", ".join(markers) + "]"


def _label(node: Node) -> str:
    if node.kind == NodeKind.ROOT.value:
        value = node.payload.get("run")
        return str(value) if isinstance(value, str) else "run"
    if node.kind == NodeKind.MODEL_CALL.value:
        value = node.payload.get("model", node.payload.get("modelId"))
        return str(value) if isinstance(value, str) else "model"
    if node.kind == NodeKind.TOOL_CALL.value:
        value = node.payload.get("tool")
        return str(value) if isinstance(value, str) else "tool"
    if node.kind == NodeKind.REFUSAL.value:
        reason = node.payload.get("reason", "refusal")
        meter = node.payload.get("meter")
        return f"{reason}:{meter}" if isinstance(meter, str) else str(reason)
    if node.payload.get("branch") is True:
        return "branch"
    for key in ("label", "checkpoint", "status"):
        value = node.payload.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return f"{key}={value}"
    return "note"


def _charges_text(node: Node) -> str:
    charges = _numeric_mapping(node.meta.get("charges"))
    return " ".join(f"{name}={charges[name]}" for name in sorted(charges))


def _sum_meta_charges(nodes: list[Node], key: str) -> dict[str, int | float]:
    totals: dict[str, Any] = {}
    for node in nodes:
        values = _numeric_mapping(node.meta.get(key))
        for name, amount in values.items():
            total = charge_to_decimal(totals.get(name, 0)) + charge_to_decimal(amount)
            totals[name] = charge_to_json(total)
    return totals


def _numeric_mapping(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): amount
        for name, amount in value.items()
        if isinstance(name, str)
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
    }


def _print_meter_group(label: str, values: object) -> None:
    print(f"{label}:")
    if not isinstance(values, Mapping) or not values:
        print("  (none)")
        return
    for name in sorted(values):
        print(f"  {name}: {values[name]}")


def _open(path: Path) -> SQLiteStore:
    if not path.is_file():
        raise FileNotFoundError(path)
    return SQLiteStore(path)


@contextmanager
def _open_store(
    spec: str,
    *,
    create: bool,
    initialize_if_missing: bool = False,
) -> Iterator[Store]:
    if create:
        _require_store_destination(
            spec,
            initialize_if_missing=initialize_if_missing,
        )
    else:
        _reject_obfuscated_remote_store_spec(spec)
    dsn, store_id = _postgres_spec(spec)
    if dsn is not None:
        try:
            with PostgresStore(dsn, store_id=store_id) as postgres_store:
                yield postgres_store
        except Exception as exc:
            if type(exc).__module__.startswith("psycopg"):
                raise OSError(f"could not access {_store_label(spec)}") from exc
            raise
        return
    url, store_id, prefix = _redis_spec(spec)
    if url is not None:
        label = _store_label(spec)
        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                redis_store = RedisStore(
                    url,
                    store_id=store_id,
                    prefix=prefix,
                    create=create and initialize_if_missing,
                )
                if caught_warnings:
                    redis_store.close()
                    raise OSError(f"could not access {label}")
        except (ImportError, PollardError):
            raise
        except Exception:
            raise OSError(f"could not access {label}") from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with redis_store as opened_store:
                    yield opened_store
        except Exception as exc:
            if isinstance(exc, Warning) or _is_redis_exception(exc):
                raise OSError(f"could not access {label}") from None
            raise
        return
    uri, database, store_id, collection_prefix = _mongo_spec(spec)
    if uri is not None:
        label = _store_label(spec)
        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                mongo_store = MongoStore(
                    uri,
                    database=database,
                    store_id=store_id,
                    collection_prefix=collection_prefix,
                    create=create and initialize_if_missing,
                )
                if caught_warnings:
                    mongo_store.close()
                    raise OSError(f"could not access {label}")
        except (ImportError, PollardError):
            raise
        except Exception:
            raise OSError(f"could not access {label}") from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with mongo_store as opened_store:
                    yield opened_store
        except Exception as exc:
            if isinstance(exc, Warning) or _is_mongodb_exception(exc):
                raise OSError(f"could not access {label}") from None
            raise
        return
    uri, auth, database, store_id = _neo4j_spec(spec)
    if uri is not None:
        if auth is None:
            raise AssertionError("Neo4j store spec did not resolve authentication")
        label = _store_label(spec)
        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                neo4j_store = Neo4jStore(
                    uri,
                    auth,
                    database=database,
                    store_id=store_id,
                    create=create and initialize_if_missing,
                )
                if caught_warnings:
                    neo4j_store.close()
                    raise OSError(f"could not access {label}")
        except (ImportError, PollardError):
            raise
        except Exception:
            raise OSError(f"could not access {label}") from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with neo4j_store as opened_store:
                    yield opened_store
        except Exception as exc:
            if isinstance(exc, Warning) or _is_neo4j_exception(exc):
                raise OSError(f"could not access {label}") from None
            raise
        return
    client_config, topic, store_id, timeout = _kafka_spec(spec)
    if client_config is not None:
        label = _store_label(spec)
        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                if create:
                    kafka_store = KafkaStore(
                        client_config,
                        topic=topic,
                        store_id=store_id,
                        read_only=False,
                        require_existing=True,
                        timeout=timeout,
                    )
                else:
                    kafka_store = KafkaStore(
                        client_config,
                        topic=topic,
                        store_id=store_id,
                        read_only=True,
                        timeout=timeout,
                    )
                if caught_warnings:
                    kafka_store.close()
                    raise OSError(f"could not access {label}")
        except (ImportError, PollardError):
            raise
        except Exception:
            raise OSError(f"could not access {label}") from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with kafka_store as opened_store:
                    yield opened_store
        except Exception as exc:
            if isinstance(exc, Warning) or _is_kafka_exception(exc):
                raise OSError(f"could not access {label}") from None
            raise
        return
    path = Path(spec)
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    with SQLiteStore(path) as sqlite_store:
        yield sqlite_store


def _postgres_spec(spec: str) -> tuple[str | None, str]:
    if spec.startswith("pg-env:"):
        reference = spec.removeprefix("pg-env:")
        variable, separator, fragment = reference.partition("#")
        if not variable:
            raise ValueError("pg-env store spec requires an environment variable")
        dsn = os.environ.get(variable)
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is not set: {variable}")
        return dsn, fragment if separator and fragment else "default"
    if spec.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(spec)
        dsn = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        return dsn, parsed.fragment or "default"
    return None, "default"


def _redis_spec(spec: str) -> tuple[str | None, str, str]:
    if not _is_redis_spec(spec):
        return None, "default", "pollard"
    _validate_redis_component(spec, "store spec")
    lowered = spec.lower()
    if lowered.startswith("redis-env:"):
        variable, store_id, prefix = _redis_env_reference(spec)
        url = os.environ.get(variable)
        if not url:
            shown = _store_label_component(variable)
            raise ValueError(f"Redis URL environment variable is not set: {shown}")
        return url, store_id, prefix
    try:
        parsed = urlsplit(spec)
    except ValueError:
        raise ValueError("invalid Redis store spec") from None
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return url, _redis_store_id(parsed.fragment), "pollard"


def _redis_env_reference(spec: str) -> tuple[str, str, str]:
    if _INVALID_PERCENT_ESCAPE.search(spec):
        raise ValueError("invalid redis-env store spec")
    try:
        parsed = urlsplit(spec)
    except ValueError:
        raise ValueError("invalid redis-env store spec") from None
    variable = parsed.path
    if parsed.netloc or not variable:
        raise ValueError("redis-env store spec requires an environment variable")
    _validate_redis_component(variable, "environment variable")
    if "?" in parsed.fragment:
        raise ValueError("redis-env query must precede the store id fragment")
    try:
        parameters = _parse_query_parameters(parsed.query)
    except ValueError as exc:
        raise ValueError("redis-env store spec has an invalid query") from exc
    if len(parameters) > 1 or (
        parameters and parameters[0][0] != "prefix"
    ):
        raise ValueError("redis-env store spec accepts only one prefix parameter")
    prefix = parameters[0][1] if parameters else "pollard"
    if not prefix:
        raise ValueError("redis-env prefix must be a non-empty string")
    _validate_redis_component(prefix, "prefix")
    return variable, _redis_store_id(parsed.fragment), prefix


def _is_redis_spec(spec: str) -> bool:
    return spec.lower().startswith(("redis-env:", "redis://", "rediss://"))


def _mongo_spec(spec: str) -> tuple[str | None, str, str, str]:
    if not _is_mongo_spec(spec):
        return None, "pollard", "default", "pollard"
    _validate_mongo_component(spec, "store spec")
    if not spec.lower().startswith("mongo-env:"):
        raise ValueError(
            "direct MongoDB URI store specs are not supported; use mongo-env:"
        )
    variable, database, store_id, collection_prefix = _mongo_env_reference(spec)
    uri = os.environ.get(variable)
    if not uri:
        shown = _store_label_component(variable)
        raise ValueError(f"MongoDB URI environment variable is not set: {shown}")
    return (
        _validated_mongo_uri(uri),
        database,
        store_id,
        collection_prefix,
    )


def _mongo_env_reference(spec: str) -> tuple[str, str, str, str]:
    if _INVALID_PERCENT_ESCAPE.search(spec):
        raise ValueError("invalid mongo-env store spec")
    try:
        parsed = urlsplit(spec)
    except ValueError:
        raise ValueError("invalid mongo-env store spec") from None
    variable = parsed.path
    if parsed.netloc or not variable:
        raise ValueError("mongo-env store spec requires an environment variable")
    _validate_mongo_component(variable, "environment variable")
    if "?" in parsed.fragment:
        raise ValueError("mongo-env query must precede the store id fragment")
    try:
        parameters = _parse_query_parameters(parsed.query)
    except ValueError as exc:
        raise ValueError("mongo-env store spec has an invalid query") from exc
    values: dict[str, str] = {}
    for name, value in parameters:
        if name not in {"database", "prefix"}:
            raise ValueError(
                "mongo-env store spec accepts only database and prefix parameters"
            )
        if name in values:
            raise ValueError(
                "mongo-env store spec accepts each namespace parameter once"
            )
        values[name] = value
    database = values.get("database", "pollard")
    collection_prefix = values.get("prefix", "pollard")
    if not database:
        raise ValueError("mongo-env database must be a non-empty string")
    if not collection_prefix:
        raise ValueError("mongo-env prefix must be a non-empty string")
    _validate_mongo_component(database, "database")
    _validate_mongo_component(collection_prefix, "prefix")
    if not _MONGO_COLLECTION_PREFIX.fullmatch(collection_prefix):
        raise ValueError(
            "mongo-env prefix must start with a letter and contain only "
            "letters, digits, and underscores"
        )
    return (
        variable,
        database,
        _mongo_store_id(parsed.fragment),
        collection_prefix,
    )


def _is_mongo_spec(spec: str) -> bool:
    return spec.lower().startswith(
        ("mongo-env:", "mongodb://", "mongodb+srv://")
    )


def _validated_mongo_uri(uri: str) -> str:
    _validate_mongo_component(uri, "URI")
    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise ValueError(
            "MongoDB URI environment variable is not a valid MongoDB URI"
        ) from None
    if parsed.scheme.lower() not in {"mongodb", "mongodb+srv"} or parsed.fragment:
        raise ValueError(
            "MongoDB URI environment variable must use mongodb:// or mongodb+srv://"
        )
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _neo4j_spec(
    spec: str,
) -> tuple[str | None, tuple[str, str] | None, str, str]:
    if not _is_neo4j_spec(spec):
        return None, None, "neo4j", "default"
    _validate_neo4j_component(spec, "store spec")
    if not spec.lower().startswith("neo4j-env:"):
        raise ValueError(
            "direct Neo4j URI store specs are not supported; use neo4j-env:"
        )
    variable, user_variable, password_variable, database, store_id = (
        _neo4j_env_reference(spec)
    )
    uri = os.environ.get(variable)
    if not uri:
        shown = _store_label_component(variable)
        raise ValueError(f"Neo4j URI environment variable is not set: {shown}")
    user = os.environ.get(user_variable)
    if not user:
        shown = _store_label_component(user_variable)
        raise ValueError(f"Neo4j user environment variable is not set: {shown}")
    password = os.environ.get(password_variable)
    if not password:
        shown = _store_label_component(password_variable)
        raise ValueError(f"Neo4j password environment variable is not set: {shown}")
    return _validated_neo4j_uri(uri), (user, password), database, store_id


def _neo4j_env_reference(spec: str) -> tuple[str, str, str, str, str]:
    if _INVALID_PERCENT_ESCAPE.search(spec):
        raise ValueError("invalid neo4j-env store spec")
    try:
        parsed = urlsplit(spec)
        variable = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("invalid neo4j-env store spec") from None
    if parsed.netloc or not variable:
        raise ValueError("neo4j-env store spec requires a URI environment variable")
    _validate_neo4j_component(variable, "URI environment variable")
    if "?" in parsed.fragment:
        raise ValueError("neo4j-env query must precede the store id fragment")
    try:
        parameters = _parse_query_parameters(parsed.query)
    except ValueError as exc:
        raise ValueError("neo4j-env store spec has an invalid query") from exc
    values: dict[str, str] = {}
    for name, value in parameters:
        if name not in {"user-env", "password-env", "database"}:
            raise ValueError(
                "neo4j-env store spec accepts only user-env, password-env, "
                "and database parameters"
            )
        if name in values:
            raise ValueError(
                "neo4j-env store spec accepts each connection parameter once"
            )
        values[name] = value
    for name in ("user-env", "password-env"):
        if name in values and not values[name]:
            raise ValueError(f"neo4j-env {name} must name an environment variable")
    if "user-env" not in values or "password-env" not in values:
        raise ValueError(
            "neo4j-env store spec requires user-env and password-env parameters"
        )
    database = values.get("database", "neo4j")
    if not database:
        raise ValueError("neo4j-env database must be a non-empty string")
    user_variable = values["user-env"]
    password_variable = values["password-env"]
    _validate_neo4j_component(user_variable, "user environment variable")
    _validate_neo4j_component(password_variable, "password environment variable")
    _validate_neo4j_component(database, "database")
    return (
        variable,
        user_variable,
        password_variable,
        database,
        _neo4j_store_id(parsed.fragment),
    )


def _is_neo4j_spec(spec: str) -> bool:
    prefix, _obfuscated = _remote_store_prefix(spec)
    return prefix == "neo4j-env:" or prefix in _NEO4J_DIRECT_PREFIXES


def _validated_neo4j_uri(uri: str) -> str:
    _validate_neo4j_component(uri, "URI")
    if _INVALID_PERCENT_ESCAPE.search(uri):
        raise ValueError("Neo4j URI environment variable is not a valid URI")
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError(
            "Neo4j URI environment variable is not a valid URI"
        ) from None
    if (
        parsed.scheme.lower() not in _NEO4J_URI_SCHEMES
        or not parsed.netloc
        or hostname is None
        or parsed.fragment
    ):
        raise ValueError(
            "Neo4j URI environment variable must use a supported Neo4j or Bolt URI"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "Neo4j URI environment variable must not contain authentication"
        )
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _kafka_spec(
    spec: str,
) -> tuple[dict[str, object] | None, str, str, int]:
    if not _is_kafka_spec(spec):
        return None, "", "default", 30
    _validate_kafka_component(spec, "store spec")
    prefix, _obfuscated = _remote_store_prefix(spec)
    if prefix != "kafka-env:":
        raise ValueError(
            "direct Kafka broker store specs are not supported; use kafka-env:"
        )
    variable, topic, store_id, timeout = _kafka_env_reference(spec)
    raw_config = os.environ.get(variable)
    if not raw_config:
        shown = _store_label_component(variable)
        raise ValueError(
            f"Kafka client config environment variable is not set: {shown}"
        )
    return (
        _validated_kafka_client_config(raw_config),
        topic,
        store_id,
        timeout,
    )


def _kafka_env_reference(spec: str) -> tuple[str, str, str, int]:
    if _INVALID_PERCENT_ESCAPE.search(spec):
        raise ValueError("invalid kafka-env store spec")
    try:
        parsed = urlsplit(spec)
        variable = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("invalid kafka-env store spec") from None
    if parsed.netloc or not variable:
        raise ValueError(
            "kafka-env store spec requires a client config environment variable"
        )
    _validate_kafka_component(variable, "client config environment variable")
    if "?" in parsed.fragment:
        raise ValueError("kafka-env query must precede the store id fragment")
    try:
        parameters = _parse_query_parameters(parsed.query)
    except ValueError:
        raise ValueError("kafka-env store spec has an invalid query") from None
    values: dict[str, str] = {}
    for name, value in parameters:
        if name not in {"topic", "timeout"}:
            raise ValueError(
                "kafka-env store spec accepts only topic and timeout parameters"
            )
        if name in values:
            raise ValueError(
                "kafka-env store spec accepts each connection parameter once"
            )
        values[name] = value
    topic = values.get("topic")
    if not topic:
        raise ValueError("kafka-env store spec requires a non-empty topic")
    _validate_kafka_component(topic, "topic")
    timeout_text = values.get("timeout", "30")
    if not re.fullmatch(r"[0-9]+", timeout_text):
        raise ValueError("kafka-env timeout must be a positive integer")
    timeout = int(timeout_text)
    try:
        finite_timeout = math.isfinite(float(timeout))
    except OverflowError:
        finite_timeout = False
    if timeout <= 0 or not finite_timeout:
        raise ValueError("kafka-env timeout must be a positive integer")
    return variable, topic, _kafka_store_id(parsed.fragment), timeout


def _validated_kafka_client_config(raw_config: str) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw_config,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Kafka client config environment variable must contain valid JSON"
        ) from None
    if not isinstance(parsed, dict):
        raise ValueError(
            "Kafka client config environment variable must contain a JSON object"
        )
    client_config: dict[str, object] = {}
    for key, value in parsed.items():
        if (
            not isinstance(key, str)
            or not key
            or any(
                character.isspace()
                or category(character).startswith("C")
                for character in key
            )
        ):
            raise ValueError("Kafka client config contains an invalid property name")
        if not (
            isinstance(value, str | bool | int | float)
            and (
                not isinstance(value, float)
                or math.isfinite(value)
            )
        ):
            raise ValueError(
                "Kafka client config values must be strings, numbers, or booleans"
            )
        client_config[key] = value
    bootstrap_servers = client_config.get("bootstrap.servers")
    if not isinstance(bootstrap_servers, str) or not bootstrap_servers.strip():
        raise ValueError(
            "Kafka client config requires a non-empty 'bootstrap.servers' string"
        )
    if "plugin.library.paths" in client_config:
        raise ValueError(
            "Kafka CLI client config does not accept plugin.library.paths"
        )
    # Native debug output can bypass the CLI's credential-safe exception and
    # warning rendering.
    client_config.pop("debug", None)
    client_config.pop("aws_debug", None)
    client_config["log_level"] = 0
    return client_config


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _is_kafka_spec(spec: str) -> bool:
    prefix, _obfuscated = _remote_store_prefix(spec)
    return prefix in {"kafka-env:", "kafka://"}


def _is_remote_store_spec(spec: str) -> bool:
    prefix, _obfuscated = _remote_store_prefix(spec)
    return prefix is not None


def _remote_store_prefix(spec: str) -> tuple[str | None, bool]:
    candidates: tuple[str, ...] = _REMOTE_STORE_PREFIXES
    normalized = ""
    obfuscated = False
    for character in spec:
        if character.isspace() or category(character).startswith("C"):
            obfuscated = True
            continue
        normalized += character.lower()
        candidates = tuple(
            prefix for prefix in candidates if prefix.startswith(normalized)
        )
        if not candidates:
            return None, False
        for prefix in candidates:
            if normalized == prefix:
                return prefix, obfuscated
    return None, False


def _reject_obfuscated_remote_store_spec(spec: str) -> None:
    prefix, obfuscated = _remote_store_prefix(spec)
    if prefix is not None and obfuscated:
        raise ValueError(
            "remote store schemes must not contain whitespace or control characters"
        )


def _require_sqlite_target(spec: str | Path, *, command: str) -> None:
    rendered = str(spec)
    _reject_obfuscated_remote_store_spec(rendered)
    if _is_remote_store_spec(rendered):
        raise ValueError(f"{command} accepts only a SQLite path")


def _require_store_destination(
    spec: str,
    *,
    initialize_if_missing: bool = False,
) -> None:
    _reject_obfuscated_remote_store_spec(spec)
    is_redis_env = spec.lower().startswith("redis-env:")
    is_mongo_env = spec.lower().startswith("mongo-env:")
    is_neo4j_env = spec.lower().startswith("neo4j-env:")
    if initialize_if_missing and not (
        is_redis_env or is_mongo_env or is_neo4j_env
    ):
        raise ValueError(
            "--initialize-if-missing can be used only with a redis-env:, "
            "mongo-env:, or neo4j-env: destination"
        )
    if _is_redis_spec(spec) and not is_redis_env:
        raise ValueError(
            "direct Redis URL store specs cannot be used as CLI destinations; "
            "use redis-env:"
        )
    if is_redis_env:
        _require_explicit_redis_destination(spec)
    if _is_mongo_spec(spec) and not is_mongo_env:
        raise ValueError(
            "direct MongoDB URI store specs cannot be used as CLI destinations; "
            "use mongo-env:"
        )
    if is_mongo_env:
        _require_explicit_mongo_destination(spec)
    if _is_neo4j_spec(spec) and not is_neo4j_env:
        raise ValueError(
            "direct Neo4j or Bolt URI store specs cannot be used as CLI "
            "destinations; use neo4j-env:"
        )
    if is_neo4j_env:
        _require_explicit_neo4j_destination(spec)
    is_kafka_env = spec.lower().startswith("kafka-env:")
    if _is_kafka_spec(spec) and not is_kafka_env:
        raise ValueError(
            "direct Kafka broker store specs cannot be used as CLI "
            "destinations; use kafka-env:"
        )
    if is_kafka_env:
        _require_explicit_kafka_destination(spec)


def _require_explicit_redis_destination(spec: str) -> None:
    _variable, store_id, prefix = _redis_env_reference(spec)
    try:
        parsed = urlsplit(spec)
    except ValueError:
        raise ValueError("invalid redis-env store spec") from None
    if not parsed.query or not parsed.fragment:
        raise ValueError(
            "Redis CLI destinations require an explicit prefix and store id"
        )
    if any(character.isspace() for character in prefix + store_id):
        raise ValueError(
            "Redis CLI destination prefix and store id must not contain whitespace"
        )


def _require_explicit_mongo_destination(spec: str) -> None:
    _variable, database, store_id, collection_prefix = _mongo_env_reference(spec)
    try:
        parsed = urlsplit(spec)
        parameters = _parse_query_parameters(parsed.query)
    except ValueError:
        raise ValueError("invalid mongo-env store spec") from None
    names = {name for name, _value in parameters}
    if names != {"database", "prefix"} or not parsed.fragment:
        raise ValueError(
            "MongoDB CLI destinations require an explicit database, prefix, "
            "and store id"
        )
    if any(
        character.isspace()
        for character in database + collection_prefix + store_id
    ):
        raise ValueError(
            "MongoDB CLI destination database, prefix, and store id must not "
            "contain whitespace"
        )


def _require_explicit_neo4j_destination(spec: str) -> None:
    (
        variable,
        user_variable,
        password_variable,
        database,
        store_id,
    ) = _neo4j_env_reference(spec)
    try:
        parsed = urlsplit(spec)
        parameters = _parse_query_parameters(parsed.query)
    except ValueError:
        raise ValueError("invalid neo4j-env store spec") from None
    names = {name for name, _value in parameters}
    if names != {"user-env", "password-env", "database"} or not parsed.fragment:
        raise ValueError(
            "Neo4j CLI destinations require explicit URI, user, and password "
            "environment references, database, and store id"
        )
    if any(
        character.isspace()
        for character in (
            variable
            + user_variable
            + password_variable
            + database
            + store_id
        )
    ):
        raise ValueError(
            "Neo4j CLI destination references, database, and store id must not "
            "contain whitespace"
        )


def _require_explicit_kafka_destination(spec: str) -> None:
    variable, topic, store_id, _timeout = _kafka_env_reference(spec)
    try:
        parsed = urlsplit(spec)
        parameters = _parse_query_parameters(parsed.query)
    except ValueError:
        raise ValueError("invalid kafka-env store spec") from None
    names = {name for name, _value in parameters}
    if (
        names not in ({"topic"}, {"topic", "timeout"})
        or not parsed.fragment
    ):
        raise ValueError(
            "Kafka CLI destinations require an explicit client config "
            "environment reference, topic, and store id"
        )
    if any(
        character.isspace()
        for character in variable + topic + store_id
    ):
        raise ValueError(
            "Kafka CLI destination reference, topic, and store id must not "
            "contain whitespace"
        )


def _is_redis_exception(exc: BaseException) -> bool:
    module = type(exc).__module__
    return module == "redis" or module.startswith("redis.")


def _is_mongodb_exception(exc: BaseException) -> bool:
    module = type(exc).__module__
    return (
        module == "pymongo"
        or module.startswith("pymongo.")
        or module == "bson"
        or module.startswith("bson.")
    )


def _is_neo4j_exception(exc: BaseException) -> bool:
    module = type(exc).__module__
    return module == "neo4j" or module.startswith("neo4j.")


def _is_kafka_exception(exc: BaseException) -> bool:
    module = type(exc).__module__
    return (
        module in {"cimpl", "_cimpl", "confluent_kafka"}
        or module.startswith(("cimpl.", "_cimpl.", "confluent_kafka."))
    )


def _redis_store_id(fragment: str) -> str:
    try:
        store_id = unquote(fragment, errors="strict") if fragment else "default"
    except UnicodeDecodeError:
        raise ValueError("Redis store id must use valid UTF-8 encoding") from None
    _validate_redis_component(store_id, "store id")
    return store_id


def _mongo_store_id(fragment: str) -> str:
    try:
        store_id = unquote(fragment, errors="strict") if fragment else "default"
    except UnicodeDecodeError:
        raise ValueError("MongoDB store id must use valid UTF-8 encoding") from None
    _validate_mongo_component(store_id, "store id")
    return store_id


def _neo4j_store_id(fragment: str) -> str:
    try:
        store_id = unquote(fragment, errors="strict") if fragment else "default"
    except UnicodeDecodeError:
        raise ValueError("Neo4j store id must use valid UTF-8 encoding") from None
    _validate_neo4j_component(store_id, "store id")
    return store_id


def _kafka_store_id(fragment: str) -> str:
    try:
        store_id = unquote(fragment, errors="strict") if fragment else "default"
    except UnicodeDecodeError:
        raise ValueError("Kafka store id must use valid UTF-8 encoding") from None
    _validate_kafka_component(store_id, "store id")
    return store_id


def _validate_redis_component(value: str, label: str) -> None:
    if any(category(character).startswith("C") for character in value):
        raise ValueError(f"Redis {label} must not contain control characters")


def _validate_mongo_component(value: str, label: str) -> None:
    if any(category(character).startswith("C") for character in value):
        raise ValueError(f"MongoDB {label} must not contain control characters")


def _validate_neo4j_component(value: str, label: str) -> None:
    if any(category(character).startswith("C") for character in value):
        raise ValueError(f"Neo4j {label} must not contain control characters")


def _validate_kafka_component(value: str, label: str) -> None:
    if any(
        character.isspace() or category(character).startswith("C")
        for character in value
    ):
        raise ValueError(
            f"Kafka {label} must not contain whitespace or control characters"
        )


def _store_label_component(value: str) -> str:
    return quote(value, safe="-._~")


def _store_label(spec: str) -> str:
    if spec.startswith("pg-env:"):
        reference = spec.removeprefix("pg-env:")
        variable, _separator, fragment = reference.partition("#")
        return f"pg-env:{variable}#{fragment or 'default'}"
    if spec.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(spec)
        return f"postgresql://{parsed.hostname or 'host'}#{parsed.fragment or 'default'}"
    if spec.lower().startswith("redis-env:"):
        variable, store_id, prefix = _redis_env_reference(spec)
        return (
            f"redis-env:{_store_label_component(variable)}"
            f"?prefix={_store_label_component(prefix)}"
            f"#{_store_label_component(store_id)}"
        )
    if spec.lower().startswith(("redis://", "rediss://")):
        parsed = urlsplit(spec)
        host = _store_label_component(parsed.hostname or "host")
        store_id = _store_label_component(_redis_store_id(parsed.fragment))
        return f"{parsed.scheme}://{host}#{store_id}"
    if spec.lower().startswith("mongo-env:"):
        variable, database, store_id, collection_prefix = _mongo_env_reference(
            spec
        )
        return (
            f"mongo-env:{_store_label_component(variable)}"
            f"?database={_store_label_component(database)}"
            f"&prefix={_store_label_component(collection_prefix)}"
            f"#{_store_label_component(store_id)}"
        )
    if spec.lower().startswith("neo4j-env:"):
        variable, _user_variable, _password_variable, database, store_id = (
            _neo4j_env_reference(spec)
        )
        return (
            f"neo4j-env:{_store_label_component(variable)}"
            f"?database={_store_label_component(database)}"
            f"#{_store_label_component(store_id)}"
        )
    if spec.lower().startswith("kafka-env:"):
        variable, topic, store_id, _timeout = _kafka_env_reference(spec)
        return (
            f"kafka-env:{_store_label_component(variable)}"
            f"?topic={_store_label_component(topic)}"
            f"#{_store_label_component(store_id)}"
        )
    return str(Path(spec))


def _emit(value: object, *, json_output: bool) -> None:
    print(_json(value) if json_output else value)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
