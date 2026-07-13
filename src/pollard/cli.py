"""Command-line inspection for Pollard SQLite recordings."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import PollardError
from .governance import export_subtree, gc, import_subtree
from .governor import charge_to_decimal, charge_to_json, recompute_charges
from .merge import merge
from .redaction import contains_redaction
from .seal import seal
from .store import Store
from .stores import PostgresStore, SQLiteStore
from .tree import Node, NodeKind
from .verify import verify


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

    show = subparsers.add_parser("show", help="render a stored run tree")
    show.add_argument("db", type=Path)
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
    report.add_argument("db", type=Path)
    report.add_argument("root_id")
    report.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    report.set_defaults(handler=_report)

    check = subparsers.add_parser("verify", help="verify stored identities and results")
    check.add_argument("db", type=Path)
    check.add_argument("root_id", nargs="?")
    check.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    check.set_defaults(handler=_verify)

    seal_parser = subparsers.add_parser("seal", help="create a subtree seal report")
    seal_parser.add_argument("db", type=Path)
    seal_parser.add_argument("root_id")
    seal_parser.add_argument("--output", type=Path, help="write the full JSON report")
    seal_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    seal_parser.set_defaults(handler=_seal)

    runs = subparsers.add_parser("runs", help="list run roots in a store")
    runs.add_argument("stores", nargs="+", help="SQLite path or PostgreSQL store spec")
    runs.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    runs.set_defaults(handler=_runs)

    gc_parser = subparsers.add_parser("gc", help="run explicit offline maintenance")
    gc_parser.add_argument("db", type=Path)
    gc_parser.add_argument(
        "mode",
        choices=("drop-pruned", "compact"),
        nargs="?",
        default="drop-pruned",
    )
    gc_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    gc_parser.set_defaults(handler=_gc)

    export_parser = subparsers.add_parser("export", help="export a sealed subtree")
    export_parser.add_argument("db", type=Path)
    export_parser.add_argument("root_id")
    export_parser.add_argument("path", type=Path)
    export_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    export_parser.set_defaults(handler=_export)

    import_parser = subparsers.add_parser("import", help="import a sealed subtree")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("db", type=Path)
    import_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    import_parser.set_defaults(handler=_import)

    merge_parser = subparsers.add_parser("merge", help="merge one or more stores")
    merge_parser.add_argument("destination", help="destination store spec")
    merge_parser.add_argument("sources", nargs="+", help="source store specs")
    merge_parser.add_argument(
        "--replay",
        action="store_true",
        help="reject result conflicts instead of recording them",
    )
    merge_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    merge_parser.set_defaults(handler=_merge)
    return parser


def _show(args: argparse.Namespace) -> int:
    with _open(args.db) as store:
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
    with _open(args.db) as store:
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
    with _open(args.db) as store:
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
    with _open(args.db) as store:
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
    with _open_store(args.destination, create=True) as destination:
        for source_spec in args.sources:
            with _open_store(source_spec, create=False) as source:
                report = merge(destination, source, replay=args.replay)
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


def _gc(args: argparse.Namespace) -> int:
    with _open(args.db) as store:
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
    with _open(args.db) as store:
        document = export_subtree(store, args.root_id, args.path).to_dict()
    if args.json:
        _emit(document, json_output=True)
    else:
        print(f"{document['path']}  {document['digest']}")
    return 0


def _import(args: argparse.Namespace) -> int:
    with SQLiteStore(args.db) as store:
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
def _open_store(spec: str, *, create: bool) -> Iterator[Store]:
    dsn, store_id = _postgres_spec(spec)
    if dsn is not None:
        try:
            with PostgresStore(dsn, store_id=store_id) as store:
                yield store
        except Exception as exc:
            if type(exc).__module__.startswith("psycopg"):
                raise OSError(f"could not access {_store_label(spec)}") from exc
            raise
        return
    path = Path(spec)
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    with SQLiteStore(path) as store:
        yield store


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


def _store_label(spec: str) -> str:
    if spec.startswith("pg-env:"):
        reference = spec.removeprefix("pg-env:")
        variable, _separator, fragment = reference.partition("#")
        return f"pg-env:{variable}#{fragment or 'default'}"
    if spec.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(spec)
        return f"postgresql://{parsed.hostname or 'host'}#{parsed.fragment or 'default'}"
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
