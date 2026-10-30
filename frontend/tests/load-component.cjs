const {readFileSync, existsSync} = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const ts = require("typescript");
const cache = new Map();
function load(relative) {
  let filename = path.resolve(__dirname, "..", relative);
  if (!path.extname(filename)) filename += existsSync(filename + ".tsx") ? ".tsx" : ".ts";
  if (cache.has(filename)) return cache.get(filename).exports;
  const loaded = new Module(filename, module);
  cache.set(filename, loaded);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = request => {
    if (request.startsWith("@/")) return load(request.slice(2));
    if (request.startsWith(".")) return load(path.relative(path.resolve(__dirname, ".."), path.resolve(path.dirname(filename), request)));
    return originalRequire(request);
  };
  loaded._compile(ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: {target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true},
  }).outputText, filename);
  return loaded.exports;
}
module.exports = {load};

