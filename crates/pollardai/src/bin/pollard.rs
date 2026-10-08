//! Native operator commands. Inspection never creates or repairs a store.
use pollardai::store_spec::StoreReference;
use pollardai::*;
use rust_decimal::{prelude::ToPrimitive, Decimal};
use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
};

const HELP:&str="pollard (native Rust)\n\nCommands:\n  runs STORE... [--json]\n  show STORE ROOT [--json|--ascii|--unicode|--html FILE] [--payloads]\n  report STORE ROOT [--json]\n  verify STORE [ROOT] [--json]\n  seal STORE ROOT [--output FILE] [--json]\n  export STORE ROOT FILE\n  import FILE SQLITE_DB [--initialize-if-missing]\n  merge --destination STORE SOURCE... [--replay] [--initialize-if-missing]\n  gc SQLITE_DB --mode drop-pruned|compact [--json]\n\nStores: SQLite path, pg-env:VAR#store, redis-env:VAR?prefix=pollard#store,\n  mongo-env:VAR?database=pollard&prefix=pollard#store,\n  neo4j-env:VAR?user-env=USER_VAR&password-env=PASSWORD_VAR#store,\n  kafka-env:VAR?topic=TOPIC&timeout=30#store. Enable matching Cargo features.\nRemote connection values remain in environment variables. JSON is the default.\n";
fn invalid(message: impl Into<String>) -> Error {
    Error::Invalid(message.into())
}
fn required<'a>(args: &'a [String], index: usize, name: &str) -> Result<&'a str> {
    args.get(index)
        .map(String::as_str)
        .ok_or_else(|| invalid(format!("missing {name}")))
}
fn take_flag(args: &mut Vec<String>, flag: &str) -> bool {
    if let Some(i) = args.iter().position(|s| s == flag) {
        args.remove(i);
        true
    } else {
        false
    }
}
fn take_option(args: &mut Vec<String>, flag: &str) -> Result<Option<String>> {
    if let Some(i) = args.iter().position(|s| s == flag) {
        args.remove(i);
        if i == args.len() || args[i].starts_with("--") {
            return Err(invalid(format!("{flag} needs a value")));
        }
        Ok(Some(args.remove(i)))
    } else {
        Ok(None)
    }
}
fn exact(args: &[String], count: usize) -> Result<()> {
    if args.len() == count {
        Ok(())
    } else {
        Err(invalid("wrong number of arguments; use --help"))
    }
}
fn emit(v: Value) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(&v).map_err(|e| invalid(e.to_string()))?
    );
    Ok(())
}
fn destination(path: &str, create: bool) -> Result<Box<dyn RecordingStore>> {
    StoreReference::parse(path)?.open(false, create)
}
fn inspection(path: &str) -> Result<Box<dyn RecordingStore>> {
    StoreReference::parse(path)?.open(true, false)
}
fn sqlite_only(path: &str) -> Result<()> {
    if matches!(StoreReference::parse(path)?, StoreReference::Remote(_)) {
        return Err(invalid("this command requires a SQLite path"));
    }
    Ok(())
}
fn totals(nodes: &[Node], key: &str) -> Result<BTreeMap<String, f64>> {
    let mut out: BTreeMap<String, Decimal> = BTreeMap::new();
    for node in nodes {
        node.validate()?;
        if let Some(charges) = node.meta.get(key).and_then(Value::as_object) {
            for (name, amount) in charges {
                if amount.is_number() {
                    let amount = amount.to_string();
                    let amount = parse_decimal_exact(&amount)
                        .map_err(|_| invalid("charge exceeds decimal range"))?;
                    let value = out.entry(name.clone()).or_default();
                    *value = checked_decimal_add(*value, amount)
                        .ok_or_else(|| invalid("invalid charge total"))?;
                }
            }
        }
    }
    out.into_iter()
        .map(|(name, amount)| {
            Ok((
                name,
                amount
                    .to_f64()
                    .ok_or_else(|| invalid("invalid charge total"))?,
            ))
        })
        .collect()
}
fn run(mut args: Vec<String>) -> Result<i32> {
    if args.is_empty() || args == ["--help"] || args == ["-h"] {
        print!("{HELP}");
        return Ok(0);
    }
    if args == ["--version"] {
        println!(
            "pollardai {} (PyPI compatibility target 1.6.0)",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(0);
    }
    let command = args.remove(0);
    take_flag(&mut args, "--json");
    match command.as_str() {
        "runs" => {
            if args.is_empty() {
                return Err(invalid("runs needs one or more databases"));
            }
            let mut runs = Vec::new();
            for path in args {
                let store = inspection(&path)?;
                for id in store.roots()? {
                    let root = store.get(&id)?;
                    let nodes = store.walk(&id)?;
                    runs.push(json!({"store":path,"root_id":id,"label":render::label(&root),"attempt":root.attempt,"nodes":nodes.len(),"pruned":nodes.iter().filter(|n|n.meta.get("pruned")==Some(&json!(true))).count()}));
                }
            }
            emit(json!({"runs":runs}))?;
        }
        "show" | "report" | "seal" | "export" => {
            let ascii = command == "show" && take_flag(&mut args, "--ascii");
            let unicode = command == "show" && take_flag(&mut args, "--unicode");
            let html = if command == "show" {
                take_option(&mut args, "--html")?
            } else {
                None
            };
            if html.is_some() && (ascii || unicode) {
                return Err(invalid("choose HTML or text rendering"));
            }
            let payloads = if command == "show" {
                take_flag(&mut args, "--payloads")
            } else {
                false
            };
            let output = if command == "seal" {
                take_option(&mut args, "--output")?
            } else {
                None
            };
            exact(&args, if command == "export" { 3 } else { 2 })?;
            let store = inspection(&args[0])?;
            let root = &args[1];
            match command.as_str() {
                "show" => {
                    if let Some(path) = html {
                        let document = render::render_html(&*store, root, payloads)?;
                        fs::write(&path, &document).map_err(|e| invalid(e.to_string()))?;
                        emit(json!({"root_id":root,"output":path,"bytes":document.len()}))?;
                        return Ok(0);
                    }
                    if ascii || unicode {
                        println!(
                            "{}",
                            render::render_ascii(&*store, root, unicode, payloads)?
                        );
                        return Ok(0);
                    }
                    emit(render::tree_document(&*store, root, payloads)?)?;
                }
                "report" => {
                    let nodes = store.walk(root)?;
                    emit(
                        json!({"root_id":root,"nodes":nodes.len(),"spent":totals(&nodes,"charges")?,"avoided":totals(&nodes,"avoided")?}),
                    )?;
                }
                "seal" => {
                    let report = seal(&*store, root)?;
                    let document =
                        serde_json::to_value(&report).map_err(|e| invalid(e.to_string()))?;
                    if let Some(path) = output {
                        fs::write(
                            &path,
                            serde_json::to_string_pretty(&document)
                                .map_err(|e| invalid(e.to_string()))?,
                        )
                        .map_err(|e| invalid(e.to_string()))?;
                        emit(json!({"root_id":root,"digest":report.digest,"output":path}))?;
                    } else {
                        emit(document)?;
                    }
                }
                _ => emit(
                    serde_json::to_value(export_subtree(&*store, root, &args[2])?)
                        .map_err(|e| invalid(e.to_string()))?,
                )?,
            }
        }
        "verify" => {
            if !(1..=2).contains(&args.len()) {
                return Err(invalid("verify needs DB and optionally ROOT"));
            }
            let store = inspection(&args[0])?;
            let roots = if args.len() == 2 {
                vec![args[1].clone()]
            } else {
                store.roots()?
            };
            let mut findings = BTreeSet::new();
            let mut count = 0;
            for root in &roots {
                for node in store.walk(root)? {
                    count += 1;
                    for f in verify(&*store, &node.id).findings {
                        findings.insert((f.node_id, f.message));
                    }
                }
            }
            let findings: Vec<_> = findings
                .into_iter()
                .map(|(node_id, message)| json!({"node_id":node_id,"message":message}))
                .collect();
            let ok = findings.is_empty();
            emit(json!({"ok":ok,"roots":roots,"nodes":count,"findings":findings}))?;
            return Ok(if ok { 0 } else { 1 });
        }
        "import" => {
            let create = take_flag(&mut args, "--initialize-if-missing");
            exact(&args, 2)?;
            sqlite_only(&args[1])?;
            // Parse and validate before opening (or creating) a destination.
            let document: Value = serde_json::from_str(
                &fs::read_to_string(&args[0]).map_err(|e| invalid(e.to_string()))?,
            )
            .map_err(|e| invalid(e.to_string()))?;
            let mut scratch = MemoryStore::new();
            import_manifest(document.clone(), &mut scratch)?;
            let mut store = destination(&args[1], create)?;
            let mut report = import_manifest(document, &mut *store)?;
            report.path = args[0].clone();
            emit(serde_json::to_value(report).map_err(|e| invalid(e.to_string()))?)?;
        }
        "merge" => {
            let path = take_option(&mut args, "--destination")?
                .ok_or_else(|| invalid("merge requires --destination DB"))?;
            let replay = take_flag(&mut args, "--replay");
            let create = take_flag(&mut args, "--initialize-if-missing");
            if args.is_empty() {
                return Err(invalid("merge requires source databases"));
            }
            let mut sources = Vec::new();
            // Fully read/validate every source before any destination write.
            for path in &args {
                let source = inspection(path)?;
                let mut snapshot = MemoryStore::new();
                merge(&mut snapshot, &*source, true)?;
                sources.push(snapshot);
            }
            let mut store = destination(&path, create)?;
            let mut reports = Vec::new();
            let mut total = MergeReport::default();
            for (path, source) in args.iter().zip(&sources) {
                let r = merge(&mut *store, source, replay)?;
                total.copied += r.copied;
                total.existing += r.existing;
                total.result_conflicts += r.result_conflicts;
                total.meta_conflicts += r.meta_conflicts;
                let mut report = serde_json::to_value(r).map_err(|e| invalid(e.to_string()))?;
                report["source"] = json!(path);
                reports.push(report);
            }
            emit(
                json!({"destination":path,"sources":reports,"copied":total.copied,"existing":total.existing,"result_conflicts":total.result_conflicts,"meta_conflicts":total.meta_conflicts}),
            )?;
        }
        "gc" => {
            let mode =
                take_option(&mut args, "--mode")?.ok_or_else(|| invalid("gc requires --mode"))?;
            exact(&args, 1)?;
            sqlite_only(&args[0])?;
            if mode != "drop-pruned" && mode != "compact" {
                return Err(invalid("unknown gc mode"));
            }
            let mut store = destination(required(&args, 0, "DB")?, false)?;
            emit(
                serde_json::to_value(gc(&mut *store, &mode)?)
                    .map_err(|e| invalid(e.to_string()))?,
            )?;
        }
        _ => return Err(invalid(format!("unknown command {command}; use --help"))),
    }
    Ok(0)
}
fn main() {
    match run(env::args().skip(1).collect()) {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    }
}
