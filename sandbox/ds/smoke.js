// Require-only smoke test for sandbox-runtime-ds Node deps.
// Run inside the image (NODE_PATH already pre-set in the Dockerfile).
// Exits 0 on success, 1 on the first failed require.

const modules = ["pptxgenjs", "docx"];
const failures = [];
for (const m of modules) {
    try {
        require(m);
    } catch (err) {
        failures.push([m, `${err.name}: ${err.message}`]);
    }
}
if (failures.length > 0) {
    console.error("node-deps: FAIL");
    for (const [m, msg] of failures) console.error(`  ${m}: ${msg}`);
    process.exit(1);
}
console.log(`node-deps: OK (${modules.length} modules)`);
