// SPDX-License-Identifier: MIT
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(name, dependencies = {}, globals = {}) {
    const file = path.resolve(__dirname, '../src/ts', name);
    const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        fileName: file, reportDiagnostics: true,
        compilerOptions: {
            target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
            jsx: ts.JsxEmit.React, esModuleInterop: true,
        },
    });
    assert.deepEqual(compiled.diagnostics, []);
    const target = { exports: {} };
    const factory = vm.runInThisContext(`(function(module, exports, require, window, document) {${compiled.outputText}\n})`, { filename: file });
    factory(target, target.exports, (dependency) => {
        if (dependency in dependencies) return dependencies[dependency];
        if (dependency === './messages') return load('messages.ts');
        throw Error(`Unexpected dependency: ${dependency}`);
    }, globals.window, globals.document);
    return target.exports;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
module.exports = { load, tick };
