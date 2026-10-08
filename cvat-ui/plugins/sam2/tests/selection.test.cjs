// SPDX-License-Identifier: MIT
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {validateRange,resolveSelection}=require(path.join(process.env.SAM2_TEST_BUILD,'selection.js'));
const {PolygonTrackAction}=require(path.join(process.env.SAM2_TEST_BUILD,'action.js'));
const {Job}=require(path.join(process.env.SAM2_TEST_BUILD,'node_modules/cvat-core-wrapper'));

test('range uses explicit inclusive end and permits the full 1000-index limit',()=>{
    assert.equal(validateRange(10,12,10,100),null);
    assert.equal(validateRange(10,1010,10,1010),null);
});
for(const [start,end] of [[0,12],[10,10],[12,11],[10,1011],[10.5,12],[10,NaN]]) {
    test(`invalid explicit range ${start}..${end}`,()=>assert.equal(typeof validateRange(start,end,10,1010),'string'));
}
test('selection preserves the requested object order and excludes other polygons',()=>{
    const candidates=[{clientID:7},{clientID:8},{clientID:9}];
    assert.deepEqual(resolveSelection(candidates,[9,7]),[candidates[2],candidates[0]]);
});
for(const ids of [[],[1,1],[1,2,3,4,5],[99]]) {
    test(`reject invalid polygon selection ${JSON.stringify(ids)}`,()=>assert.throws(()=>resolveSelection([{clientID:1}],ids)));
}
test('SAM3.1 uses its own deployed function, never silently falls back to SAM2',async()=>{
    const job=new Job(); job.dimension='2d';
    const core={lambda:{list:async()=>({models:[{id:'pth-sam2-tracker',kind:'tracker'}]})}};
    const action=new PolygonTrackAction(core,{name:'SAM3.1',functionID:'pth-sam31-tracker'});
    assert.equal(action.name,'SAM3.1: track polygon shapes');
    await assert.rejects(()=>action.init(job,{'Frames to track':2}),/pth-sam31-tracker/);
    core.lambda.list=async()=>({models:[{id:'pth-sam31-tracker',kind:'tracker'}]});
    await action.init(job,{'Frames to track':2}); await action.destroy();
});
test('rotated polygons are not offered as seeds',()=>{
    const action=new PolygonTrackAction({});
    assert.equal(action.isApplicableForObject({objectType:'shape',shapeType:'polygon',outside:false,lock:false,rotation:15}),false);
});
