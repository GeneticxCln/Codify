use std::path::Path;

fn watch_tree(path: &Path) {
    println!("cargo:rerun-if-changed={}", path.display());
    let Ok(entries) = std::fs::read_dir(path) else {
        return;
    };
    let mut children: Vec<_> = entries.flatten().map(|e| e.path()).collect();
    children.sort();
    for child in children {
        if child.is_dir() {
            watch_tree(&child);
        } else {
            println!("cargo:rerun-if-changed={}", child.display());
        }
    }
}

fn main() {
    // Re-run when the *frontend* changes, not just when Rust does.
    //
    // `tauri::generate_context!` lives in `lib.rs` and embeds `frontendDist`
    // (../ui/dist) into the binary at compile time. Cargo tracks that by the
    // mtime of the crate that invokes the macro — and if only the .ts/.tsx files
    // change, lib.rs is untouched, so cargo considers the crate fresh and the
    // binary keeps serving the *previous* ui/dist. That is not a build that is
    // slow to notice; it is a build that silently ships the old frontend while
    // every source-level test passes against the new one.
    //
    // Emitting `../ui/dist` and every file/directory beneath it as build-script
    // inputs ensures nested asset changes and in-place edits (e.g. `index.html`)
    // also invalidate the build script and force lib.rs to re-embed the new assets.
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    watch_tree(Path::new("../ui/dist"));
    tauri_build::build()
}
