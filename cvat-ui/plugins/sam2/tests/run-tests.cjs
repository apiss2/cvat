// SPDX-License-Identifier: MIT
// Compile action/orchestration against a small CVAT contract fixture. The React
// dialog additionally needs a real CVAT UI build and browser integration test.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'cvat-sam2-tests-'));
const fixture = `
declare module 'cvat-core-wrapper' {
    export enum ActionParameterType { NUMBER='number' }
    export enum ObjectType { SHAPE='shape', TRACK='track', TAG='tag' }
    export enum ShapeType { POLYGON='polygon', MASK='mask', RECTANGLE='rectangle' }
    export enum Source { SEMI_AUTO='semi-auto', AUTO='auto', MANUAL='manual' }
    interface Attr { spec_id:number; value:string; }
    interface Shape { id?:number; clientID?:number; label_id:number; group:number; frame:number; source:Source; attributes:Attr[]; elements:Shape[]; occluded:boolean; outside:boolean; points?:number[]; rotation:number; z_order:number; type:ShapeType; }
    interface Track { id?:number; clientID?:number; label_id:number; group:number; frame:number; source:Source; attributes:Attr[]; elements:Track[]; shapes:{ attributes:Attr[]; id?:number; points?:number[]; frame:number; occluded:boolean; outside:boolean; rotation:number; type:ShapeType; z_order:number; }[]; }
    interface Collection { shapes:Shape[]; tracks:Track[]; tags:unknown[]; }
    interface Input { onProgress(message:string,percent:number):void; cancelled():boolean; collection:Collection; frameData:{width:number;height:number;number:number}; }
    interface Output { created:Collection; deleted:Collection; }
    interface Parameters { [name:string]:{type:ActionParameterType;values:string[]|((arg:{instance:Job|Task})=>string[]);defaultValue:string|((arg:{instance:Job|Task})=>string)}; }
    export abstract class BaseCollectionAction {
        abstract init(instance:Job|Task,parameters:Record<string,string|number>):Promise<void>;
        abstract destroy():Promise<void>;
        abstract run(input:Input):Promise<Output>;
        abstract applyFilter(input:Pick<Input,'collection'|'frameData'>):Collection;
        abstract isApplicableForObject(state:ObjectState):boolean;
        abstract get name():string;
        abstract get parameters():Parameters|null;
    }
    export interface ObjectState { clientID:number;objectType:ObjectType;shapeType:ShapeType;outside:boolean;lock:boolean;rotation:number; export():Promise<Shape|Track>; }
    export class Task { size:number; }
    export class Job {
        id:number;taskId:number;dimension:'2d'|'3d';stopFrame:number;
        labels:{id:number;attributes:{id:number;mutable:boolean}[]}[];
        annotations:{get(frame:number,allTracks:boolean,filters:object[]):Promise<ObjectState[]>};
        frames:{frameNumbers():Promise<number[]>;get(frame:number):Promise<{width:number;height:number;deleted:boolean}>};
    }
    export interface MLModel { id:string;kind:string; }
    export interface CVATCore {
        lambda:{list():Promise<{models:MLModel[];count:number}>;call(taskID:number,model:MLModel,args:unknown):Promise<unknown>};
        actions:{register(action:BaseCollectionAction):Promise<void>;unregister(action:BaseCollectionAction):Promise<void>};
    }
}
`;
try {
    fs.writeFileSync(path.join(temp,'fixture.d.ts'), fixture);
    const source = path.resolve(__dirname, '../src/ts');
    execFileSync(process.env.TSC || 'tsc', ['--strict','--target','ES2022','--module','commonjs',
        '--skipLibCheck','--rootDir',source,'--outDir',path.join(temp,'built'),
        path.join(temp,'fixture.d.ts'),...['tracking.ts','action.ts','selection.ts'].map(name=>path.join(source,name))],{stdio:'inherit'});
    console.log('PASS: TypeScript strict action/orchestration contract compile (not full CVAT UI).');
    const modules=path.join(temp,'built','node_modules','cvat-core-wrapper');
    fs.mkdirSync(modules,{recursive:true});
    fs.writeFileSync(path.join(modules,'index.js'), `
        class BaseCollectionAction {} class Job {} class Task {}
        module.exports={BaseCollectionAction,Job,Task,ActionParameterType:{NUMBER:'number'},
            ObjectType:{SHAPE:'shape',TRACK:'track',TAG:'tag'},ShapeType:{POLYGON:'polygon',MASK:'mask',RECTANGLE:'rectangle'},
            Source:{SEMI_AUTO:'semi-auto',AUTO:'auto',MANUAL:'manual'}};
    `);
    execFileSync(process.execPath, ['--test',...['tracking.test.cjs','action.test.cjs','selection.test.cjs'].map(name=>path.join(__dirname,name))],{
        stdio:'inherit',env:{...process.env,SAM2_TEST_BUILD:path.join(temp,'built')},
    });
} finally { fs.rmSync(temp,{recursive:true,force:true}); }
