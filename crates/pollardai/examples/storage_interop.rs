//! Copy a Python-compatible SQLite recording and append one native Rust result.
use pollardai::*;

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if ![4, 5].contains(&args.len()) {
        return Err(Error::Invalid(
            "usage: storage_interop SOURCE.db DESTINATION.db OUTPUT.json [CUSTODY.db]".into(),
        ));
    }
    let source = SQLiteStore::open_read_only(&args[1])?;
    let roots = source.roots()?;
    let mut destination = SQLiteStore::open(&args[2])?;
    let report = merge(&mut destination, &source, true)?;
    for root in &roots {
        if seal(&source, root)? != seal(&destination, root)? {
            return Err(Error::Integrity(
                "seal changed during SQLite interchange".into(),
            ));
        }
    }
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"rust-native"}),
        None,
        json!({}),
    )?;
    destination.put(root.clone())?;
    let node = Node::make(
        NodeKind::ModelCall,
        Some(&root.id),
        0,
        json!({"model":"local","prompt":"Unicode: héllo 🌳"}),
        Some(
            json!({"text":"native result","number":1.25,"usage":{"input_tokens":2,"output_tokens":3}}),
        ),
        json!({"charges":{"steps":1,"tokens":5}}),
    )?;
    destination.put(node.clone())?;
    export_subtree(&destination, &root.id, &args[3])?;
    if let Some(path) = args.get(4) {
        SQLiteSealSink::open(path)?.publish(
            &seal(&destination, &root.id)?,
            "interop-store",
            "rust-signer",
            Some("2026-10-08T12:00:00Z"),
        )?;
    }
    println!(
        "{}",
        json!({"copied":report.copied,"source_roots":roots,"native_root":root.id,"native_node":node.id,"native_seal":seal(&destination,&root.id)?.digest})
    );
    Ok(())
}
