// SPDX-License-Identifier: MIT
// Compile against the narrow v2.75.0 plugin contract, not the complete CVAT application.
const fs = require('node:fs'); const os = require('node:os'); const path = require('node:path');
const { execFileSync } = require('node:child_process');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'model-registry-ts-'));
try {
    const source = path.resolve(__dirname, '../src/ts');
    fs.writeFileSync(path.join(dir, 'fixture.d.ts'), `
        declare module 'components/plugins-entrypoint' {
            interface Model {id:string|number;name:string;version:number}
            interface Args {
                core:{lambda:{list():Promise<{models:Model[],count:number}>}};
                store:{getState():{auth:{user:{id:number}|null}}};
                dispatch:(action:unknown)=>unknown;
                actionCreators:{
                    getModelsSuccess:(models:Model[],count:number)=>unknown;
                    addUIComponent:(path:string,component:()=>unknown,data?:{weight?:number})=>unknown;
                    removeUIComponent:(path:string,component:()=>unknown)=>unknown;
                };
            }
            export type ComponentBuilder=(args:Args)=>{name:string;destructor:CallableFunction;globalStateDidUpdate?:CallableFunction};
            export type PluginEntryPoint=(builder:ComponentBuilder)=>void;
        }
    `);
    execFileSync(process.env.TSC || 'tsc', ['--strict','--target','ES2022','--module','commonjs','--jsx','react','--skipLibCheck','--rootDir',source,'--outDir',path.join(dir,'built'),path.join(dir,'fixture.d.ts'),path.join(source,'refresh.ts'),path.join(source,'index.tsx')], {stdio:'inherit'});
    console.log('PASS: strict compile against the CVAT plugin contract fixture (not a full CVAT build).');
    execFileSync(process.execPath, ['--test',path.join(__dirname,'refresh.test.cjs'),path.join(__dirname,'menu.test.cjs')], {stdio:'inherit',env:{...process.env,MODEL_REGISTRY_TEST_BUILD:path.join(dir,'built')}});
} finally {fs.rmSync(dir,{recursive:true,force:true});}
