// SPDX-License-Identifier: MIT
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
module.exports = function load(name, dependencies = {}) {
    const file = path.resolve(__dirname, '../src/ts', name);
    const module = { exports: {} };
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.React },
    });
    if (output.diagnostics.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(output.diagnostics, {
        getCurrentDirectory: () => __dirname, getCanonicalFileName: (file) => file, getNewLine: () => '\n',
    }));
    const factory = vm.runInThisContext(`(function(module, exports, require) {${output.outputText}\n})`, { filename: file });
    factory(module, module.exports, (name) => {
        if (!(name in dependencies)) throw new Error(`Unexpected dependency: ${name}`);
        return dependencies[name];
    });
    return module.exports;
};
