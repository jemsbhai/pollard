use pollardai::*;
use std::{
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};
static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "pollard-rust-cli-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_string_lossy().into_owned()
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn native_cli_renders_escaped_html_ascii_and_decimal_totals() {
    let temp = Temp::new();
    let db = temp.path("render.db");
    let html = temp.path("tree.html");
    let mut store = SQLiteStore::open(&db).unwrap();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"<script>alert('x')</script>"}),
        None,
        json!({}),
    )
    .unwrap();
    store.put(root.clone()).unwrap();
    for (i, usd) in [(0, 0.1), (1, 0.2)] {
        store
            .put(
                Node::make(
                    NodeKind::Note,
                    Some(&root.id),
                    0,
                    json!({"i":i,"private":"sensitive-text"}),
                    None,
                    json!({"charges":{"usd":usd}}),
                )
                .unwrap(),
            )
            .unwrap();
    }
    drop(store);
    assert_eq!(
        success(&["report", &db, &root.id])["spent"]["usd"],
        json!(0.3)
    );
    success(&["show", &db, &root.id, "--html", &html]);
    let document = std::fs::read_to_string(&html).unwrap();
    assert!(document.contains("&lt;script&gt;"));
    assert!(!document.contains("<script>"));
    assert!(!document.contains("sensitive-text"));
    success(&["show", &db, &root.id, "--html", &html, "--payloads"]);
    assert!(std::fs::read_to_string(&html)
        .unwrap()
        .contains("sensitive-text"));
    let text = cli(&["show", &db, &root.id, "--ascii"]);
    assert!(text.status.success());
    assert!(String::from_utf8(text.stdout).unwrap().contains("|-- note"));
    let text = cli(&["show", &db, &root.id, "--unicode"]);
    assert!(text.status.success());
    assert!(String::from_utf8(text.stdout).unwrap().contains("├─ note"));
}
fn cli(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_pollard"))
        .args(args)
        .output()
        .unwrap()
}

#[test]
fn cli_report_refuses_an_unrepresentable_exact_charge_total() {
    let temp = Temp::new();
    let db = temp.path("decimal-total.db");
    let mut store = SQLiteStore::open(&db).unwrap();
    let root = Node::make(
        NodeKind::Root,
        None,
        0,
        json!({"run":"exact-total"}),
        None,
        json!({}),
    )
    .unwrap();
    store.put(root.clone()).unwrap();
    for (index, text) in ["79228162514264337593543950335", "1e-28"]
        .iter()
        .enumerate()
    {
        let amount: Value = serde_json::from_str(text).unwrap();
        store
            .put(
                Node::make(
                    NodeKind::Note,
                    Some(&root.id),
                    0,
                    json!({"index":index}),
                    None,
                    json!({"charges":{"usd":amount}}),
                )
                .unwrap(),
            )
            .unwrap();
    }
    drop(store);
    let result = cli(&["report", &db, &root.id]);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("invalid charge total"));
}
fn success(args: &[&str]) -> Value {
    let out = cli(args);
    assert!(
        out.status.success(),
        "{:?}: {}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

#[test]
fn cli_tree_json_and_text_match_the_published_python_contract() {
    let fixture: Value = serde_json::from_str(include_str!("pypi160_cli.json")).unwrap();
    let temp = Temp::new();
    let db = temp.path("python-cli.db");
    let mut store = SQLiteStore::open(&db).unwrap();
    for record in fixture["records"].as_array().unwrap() {
        let node = Node::from_storage(
            record["id"].as_str().unwrap().into(),
            record["parent"].as_str().map(str::to_owned),
            serde_json::from_value(record["kind"].clone()).unwrap(),
            record["attempt"].as_u64().unwrap(),
            &record["payload"].to_string(),
            record["result_text"].as_str().map(str::to_owned),
            record["result_digest"].as_str().map(str::to_owned),
            &record["meta"].to_string(),
        )
        .unwrap();
        store.put(node).unwrap();
    }
    drop(store);
    let root = fixture["root_id"].as_str().unwrap();
    assert_eq!(success(&["show", &db, root, "--json"]), fixture["public"]);
    assert_eq!(
        success(&["show", &db, root, "--json", "--payloads"]),
        fixture["payloads"]
    );
    assert_eq!(
        success(&["runs", &db])["runs"][0]["label"],
        fixture["root_label"]
    );
    for format in ["ascii", "unicode"] {
        let result = cli(&["show", &db, root, &format!("--{format}")]);
        assert!(result.status.success());
        assert_eq!(
            String::from_utf8(result.stdout).unwrap().trim_end(),
            fixture[format].as_str().unwrap()
        );
    }
}

#[test]
fn native_cli_inspects_exports_imports_and_merges_sqlite() {
    let temp = Temp::new();
    let db = temp.path("source.db");
    let exported = temp.path("tree.json");
    let dst = temp.path("dest.db");
    let rt = Runtime::new(SQLiteStore::open(&db).unwrap(), ReplayMode::Record);
    let mut run = rt.run("cli", None, 0).unwrap();
    let root = run.root_id().to_owned();
    let call = run
        .model_call(
            json!({"secret":"sensitive-text"}),
            CallOptions::default(),
            |_| Ok(json!({"text":"ok","usage":{"input_tokens":2,"output_tokens":3}})),
        )
        .unwrap();
    drop(run);
    drop(rt);
    let listing = success(&["runs", &db, "--json"]);
    assert_eq!(listing["runs"][0]["root_id"], root);
    let display = success(&["show", &db, &root, "--json"]);
    assert_eq!(display["nodes"].as_array().unwrap().len(), 2);
    assert!(!display.to_string().contains("sensitive-text"));
    let display = success(&["show", &db, &root, "--payloads"]);
    assert!(display.to_string().contains("sensitive-text"));
    assert_eq!(success(&["report", &db, &root])["spent"]["tokens"], 5.0);
    assert_eq!(success(&["verify", &db])["ok"], true);
    let seal = success(&["seal", &db, &root]);
    assert_eq!(
        success(&["export", &db, &root, &exported])["digest"],
        seal["digest"]
    );
    assert!(!cli(&["import", &exported, &dst]).status.success());
    assert!(!std::path::Path::new(&dst).exists());
    assert_eq!(
        success(&["import", &exported, &dst, "--initialize-if-missing"])["imported"],
        2
    );
    assert_eq!(success(&["seal", &dst, &root])["digest"], seal["digest"]);
    assert_eq!(
        success(&["merge", "--destination", &dst, &db, "--replay"])["existing"],
        2
    );
    let store = SQLiteStore::open_read_only(&dst).unwrap();
    assert_eq!(
        store.get(&call.id).unwrap().result_digest,
        call.result_digest
    );
    drop(store);
    assert_eq!(
        success(&["gc", &dst, "--mode", "compact"])["removed_nodes"],
        0
    );
}

#[test]
fn cli_fails_without_repairing_or_creating_input_stores() {
    let temp = Temp::new();
    let missing = temp.path("missing.db");
    assert!(!cli(&["verify", &missing]).status.success());
    assert!(!std::path::Path::new(&missing).exists());
    let bad = temp.path("bad.json");
    std::fs::write(&bad, "{}").unwrap();
    assert!(!cli(&["import", &bad, &missing, "--initialize-if-missing"])
        .status
        .success());
    assert!(!std::path::Path::new(&missing).exists());
    assert!(!cli(&["gc", &missing, "--mode", "unknown"]).status.success());
    assert!(!std::path::Path::new(&missing).exists());
    assert!(cli(&["--help"]).status.success());
    assert!(cli(&["--version"]).status.success());
}
