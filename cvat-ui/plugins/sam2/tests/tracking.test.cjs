// SPDX-License-Identifier: MIT
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {track}=require(path.join(process.env.SAM2_TEST_BUILD,'tracking.js'));
const polygon={type:'polygon',points:[2,2,10,2,10,10,2,10]};
const input=()=>({seeds:[structuredClone(polygon)],start:10,stop:15,span:3,frameNumbers:[10,11,12,13,14,15],width:24,height:20});
function transport(options={}){
    const calls=[],frames=[];
    return {calls,frames,
        frame:async n=>{frames.push(n); options.onFrame?.(n); return {width:24,height:20,deleted:options.deleted?.includes(n)||false,...(options.meta?.[n]||{})};},
        call:async args=>{
            calls.push(structuredClone(args)); options.onCall?.(args);
            if(options.errorAt===args.frame) throw new Error('server failure');
            if(options.reply) return options.reply(args,calls.length);
            return {states:[`signed-state-${calls.length}`],shapes:[options.absent?.includes(args.frame)?null:structuredClone(polygon)]};
        }
    };
}
const run=(i,t,c=()=>false)=>track(i,t,c,()=>{});

test('forward tracker uses shapes once, signed states thereafter, closes track at boundary',async()=>{
    const t=transport(),i=input(),snapshot=structuredClone(i);const result=await run(i,t);
    assert.deepEqual(t.calls.map(c=>c.frame),[10,11,12,13]);
    assert.deepEqual(t.calls[0],{frame:10,shapes:[polygon]});
    assert.deepEqual(t.calls[1],{frame:11,states:['signed-state-1']});
    assert.deepEqual(result[0].map(k=>k.frame),[10,11,12,13,14]);
    assert.equal(result[0].at(-1).outside,true);assert.deepEqual(i,snapshot);
});
test('sparse job numbering, nonzero start, deleted frames and deleted boundary',async()=>{
    const i={...input(),span:5,stop:20,frameNumbers:[20,10,12,12,15,18]}; const t=transport({deleted:[12,18]});
    const result=await run(i,t); assert.deepEqual(t.calls.map(c=>c.frame),[10,15]);
    assert.deepEqual(result[0].map(k=>k.frame),[10,15,20]); assert.equal(result[0].at(-1).outside,true);
});
test('disappearance and reappearance retain identity and prior geometry',async()=>{
    const result=await run(input(),transport({absent:[11]}));
    assert.equal(result[0][1].outside,true); assert.equal(result[0][2].outside,false);
    assert.deepEqual(result[0][1].points,polygon.points);
});
test('job stop has no out-of-job keyframe',async()=>{
    const result=await run({...input(),span:127},transport());
    assert.equal(result[0].at(-1).frame,15); assert.equal(result[0].at(-1).outside,false);
});
test('exact seed geometry is retained even when model returns another contour',async()=>{
    const t=transport({reply:(args,n)=>({states:[`s${n}`],shapes:[{type:'polygon',points:[1,1,5,1,5,5]}]})});
    const result=await run(input(),t);assert.deepEqual(result[0][0].points,polygon.points);
});
test('cancel before first call',async()=>{
    const t=transport();assert.equal(await run(input(),t,()=>true),null);assert.equal(t.calls.length,0);
});
test('cancel during initialization discards all results',async()=>{
    let cancelled=false;const t=transport({onCall:()=>{cancelled=true;}});
    assert.equal(await run(input(),t,()=>cancelled),null);assert.equal(t.calls.length,1);
});
test('cancel during a later request discards all results',async()=>{
    let cancelled=false;const t=transport({onCall:args=>{if(args.frame===12)cancelled=true;}});
    assert.equal(await run(input(),t,()=>cancelled),null);assert.equal(t.calls.length,3);
});
test('cancel while checking boundary discards completed inference results',async()=>{
    let cancelled=false;const t=transport({onFrame:n=>{if(n===14)cancelled=true;}});
    assert.equal(await run(input(),t,()=>cancelled),null);
});
test('frame-size change stops before mismatched frame inference',async()=>{
    const t=transport({meta:{12:{width:30}}});await assert.rejects(()=>run(input(),t),/dimensions/);
    assert.deepEqual(t.calls.map(c=>c.frame),[10,11]);
});
test('deleted seed is rejected before initialization',async()=>{
    const t=transport({deleted:[10]});await assert.rejects(()=>run(input(),t),/seed frame/);assert.equal(t.calls.length,0);
});
test('all later frames deleted is an error, not a success',async()=>{
    await assert.rejects(()=>run(input(),transport({deleted:[11,12,13]})),/All later frames/);
});
test('out-of-range seed rejected',async()=>{
    await assert.rejects(()=>run({...input(),start:1},transport()),/does not belong/);
});
test('seed at end of job rejected',async()=>{
    await assert.rejects(()=>run({...input(),start:15},transport()),/no later frames/);
});
for(const span of [0,-1,1001,1.2,NaN]) test(`invalid span ${span}`,async()=>{
    await assert.rejects(()=>run({...input(),span},transport()),/1..1000/);
});
test('invalid state/shape counts rejected',async()=>{
    await assert.rejects(()=>run(input(),transport({reply:()=>({states:[],shapes:[polygon]})})),/response/);
});
test('unsigned state objects are never accepted by UI',async()=>{
    await assert.rejects(()=>run(input(),transport({reply:()=>({states:[{id:'token'}],shapes:[polygon]})})),/signed states/);
});
test('bad polygon response rejected',async()=>{
    await assert.rejects(()=>run(input(),transport({reply:()=>({states:['signed'],shapes:[{type:'mask',points:[1,2,3]}]})})),/polygon/);
});
test('inference failure rejects instead of returning partial trajectories',async()=>{
    const i=input(),snapshot=structuredClone(i);await assert.rejects(()=>run(i,transport({errorAt:12})),/server failure/);
    assert.deepEqual(i,snapshot);
});
test('multiple objects keep consistent state and trajectory order',async()=>{
    const i=input();i.seeds.push(structuredClone(polygon));
    const t=transport({reply:()=>({states:['a','b'],shapes:[polygon,null]})});
    const result=await run(i,t);assert.equal(result.length,2);assert.equal(result[1][1].outside,true);
    assert.deepEqual(t.calls[1].states,['a','b']);
});
test('object limit enforced without requests',async()=>{
    const i=input();i.seeds=Array(5).fill(polygon);const t=transport();
    await assert.rejects(()=>run(i,t),/one and four/);assert.equal(t.calls.length,0);
});

const {continueWithRetry}=require(path.join(process.env.SAM2_TEST_BUILD,'tracking.js'));

test('lost continuation response retries the identical frame and signed states',async()=>{
    const args={frame:17,states:['signed-previous']}, calls=[];
    const t={wait:async()=>{},call:async value=>{
        calls.push(structuredClone(value));
        if(calls.length===1) throw Object.assign(new Error('gateway restart'),{code:502});
        return {states:['signed-next'],shapes:[polygon]};
    }};
    const result=await continueWithRetry(args,t,()=>false);
    assert.deepEqual(calls,[args,args]); assert.equal(result.states[0],'signed-next');
});

test('three failed continuation attempts stop without partial annotations',async()=>{
    let calls=0;
    const t={wait:async()=>{},call:async()=>{calls++;throw Object.assign(new Error('down'),{code:503});}};
    await assert.rejects(()=>continueWithRetry({frame:1,states:['s']},t,()=>false),/down/);
    assert.equal(calls,3);
});

test('definitive conflict is not retried',async()=>{
    let calls=0;
    const t={wait:async()=>{},call:async()=>{calls++;throw Object.assign(new Error('conflict'),{code:409});}};
    await assert.rejects(()=>continueWithRetry({frame:1,states:['s']},t,()=>false),/conflict/);
    assert.equal(calls,1);
});

test('cancel during retry backoff sends no further request',async()=>{
    let cancelled=false,calls=0;
    const t={wait:async()=>{cancelled=true;},call:async()=>{calls++;throw Object.assign(new Error('down'),{code:504});}};
    assert.equal(await continueWithRetry({frame:1,states:['s']},t,()=>cancelled),null);
    assert.equal(calls,1);
});

test('initialization is never retried automatically',async()=>{
    let calls=0;
    const t=transport();t.call=async()=>{calls++;throw Object.assign(new Error('init failed'),{code:503});};
    await assert.rejects(()=>run(input(),t),/init failed/);assert.equal(calls,1);
});

test('more than 127 frames can be processed with bounded per-action range',async()=>{
    const i={...input(),start:0,stop:141,span:140,frameNumbers:Array.from({length:142},(_,n)=>n)};
    const result=await run(i,transport());assert.equal(result[0].length,142);
});

test('large result exceeds coordinate budget without returning partial tracks',async()=>{
    const huge={type:'polygon',points:Array.from({length:20000},(_,n)=>n%2?10:8)};
    const i={...input(),start:0,stop:200,span:150,frameNumbers:Array.from({length:201},(_,n)=>n)};
    const t=transport({reply:()=>({states:['s'],shapes:[huge]})});
    await assert.rejects(()=>run(i,t),/too large/);
});
