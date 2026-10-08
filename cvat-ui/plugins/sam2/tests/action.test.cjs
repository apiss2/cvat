// SPDX-License-Identifier: MIT
const {test}=require('node:test');const assert=require('node:assert/strict');const path=require('node:path');
const build=process.env.SAM2_TEST_BUILD;
const {SAM2TrackAction}=require(path.join(build,'action.js'));
const {Job,Task}=require(path.join(build,'node_modules/cvat-core-wrapper'));
function setup(){
    const seed={id:99,clientID:7,label_id:3,group:5,frame:10,source:'manual',attributes:[{spec_id:1,value:'red'},{spec_id:2,value:'visible'}],
        elements:[],occluded:true,outside:false,points:[2,2,10,2,10,10,2,10],rotation:0,z_order:4,type:'polygon'};
    const state={clientID:7,objectType:'shape',shapeType:'polygon',outside:false,lock:false,export:async()=>structuredClone(seed)};
    const job=new Job();Object.assign(job,{id:11,taskId:22,dimension:'2d',stopFrame:14,
        labels:[{id:3,attributes:[{id:1,mutable:false},{id:2,mutable:true}]}],
        frames:{frameNumbers:async()=>[10,11,12,13,14],get:async()=>({width:24,height:20,deleted:false})},
        annotations:{get:async()=>[state]},});
    const calls=[];const core={lambda:{
        list:async()=>({models:[{id:'pth-sam2-tracker',kind:'tracker'}],count:1}),
        call:async(task,model,args)=>{calls.push({task,model,args});return {states:[`signed${calls.length}`],shapes:[{type:'polygon',points:seed.points}]};}
    }};
    const action=new SAM2TrackAction(core);
    const input={collection:{shapes:[structuredClone(seed)],tracks:[],tags:[]},frameData:{number:10,width:24,height:20},cancelled:()=>false,onProgress:()=>{}};
    return {seed,state,job,core,calls,action,input};
}
test('action yields one atomic replacement with preserved labels, groups and attributes',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});const snapshot=structuredClone(s.input.collection);
    const result=await s.action.run(s.input);const track=result.created.tracks[0];
    assert.equal(track.label_id,3);assert.equal(track.group,5);assert.equal(track.source,'semi-auto');
    assert.deepEqual(track.attributes,[{spec_id:1,value:'red'}]);
    assert.deepEqual(track.shapes[0].attributes,[{spec_id:2,value:'visible'}]);
    assert.equal(track.shapes[0].occluded,true);assert.equal(track.shapes[0].z_order,4);
    assert.equal(track.id,undefined);assert.equal(track.clientID,undefined);assert.equal(track.shapes[0].id,undefined);
    assert.equal(track.shapes.at(-1).frame,13);assert.equal(track.shapes.at(-1).outside,true);
    assert.deepEqual(result.deleted.shapes,snapshot.shapes);assert.deepEqual(s.input.collection,snapshot);
    assert(s.calls.every(c=>c.task===22&&c.args.job===11));
});
test('existing tracks and nonpolygon shapes are never selected for replacement',()=>{
    const s=setup();const mask={...s.seed,type:'mask'},rotated={...s.seed,rotation:20};
    const input={frameData:s.input.frameData,collection:{shapes:[s.seed,mask,rotated],tracks:[{clientID:8}],tags:[{}]}};
    const filtered=s.action.applyFilter(input);assert.deepEqual(filtered,{shapes:[s.seed],tracks:[],tags:[]});
});
test('cancelled action returns no created or deleted annotations',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});s.input.cancelled=()=>true;
    const result=await s.action.run(s.input);assert.deepEqual(result,{created:{shapes:[],tracks:[],tags:[]},deleted:{shapes:[],tracks:[],tags:[]}});
    assert.equal(s.calls.length,0);
});
test('locked seed rejected before inference',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});s.state.lock=true;
    await assert.rejects(()=>s.action.run(s.input),/locked/);assert.equal(s.calls.length,0);
});
test('concurrent seed edit is detected before returning commit data',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});const call=s.core.lambda.call;
    s.core.lambda.call=async(...args)=>{const result=await call(...args);if(args[2].frame===12)s.seed.group=6;return result;};
    await assert.rejects(()=>s.action.run(s.input),/seed changed/);
    assert.equal(s.input.collection.shapes[0].group,5);
});
test('server failure never returns a partial annotation replacement',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});s.core.lambda.call=async()=>{throw new Error('timeout');};
    await assert.rejects(()=>s.action.run(s.input),/timeout/);assert.equal(s.input.collection.shapes.length,1);
});
test('missing deployed tracker is reported',async()=>{
    const s=setup();s.core.lambda.list=async()=>({models:[],count:0});
    await assert.rejects(()=>s.action.init(s.job,{'Frames to track':2}),/Deploy/);
});
test('reject non-job and 3D sessions',async()=>{
    const s=setup();await assert.rejects(()=>s.action.init(new Task(),{}),/job/);
    s.job.dimension='3d';await assert.rejects(()=>s.action.init(s.job,{}),/2D/);
});
test('destroy clears active session references',async()=>{
    const s=setup();await s.action.init(s.job,{'Frames to track':2});await s.action.destroy();
    await assert.rejects(()=>s.action.run(s.input),/not initialized/);
});
