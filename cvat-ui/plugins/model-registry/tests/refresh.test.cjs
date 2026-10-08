// SPDX-License-Identifier: MIT
const test=require('node:test');const assert=require('node:assert/strict');const path=require('node:path');
const {createRefresher}=require(path.join(process.env.MODEL_REGISTRY_TEST_BUILD,'refresh.js'));
function setup(){let identity='1',visible=true,rows=[{id:'a'}],loader=null;const commits=[],errors=[];
    const r=createRefresher({identity:()=>identity,visible:()=>visible,load:()=>loader?loader():Promise.resolve(rows),fingerprint:JSON.stringify,commit:v=>commits.push(v),failed:e=>errors.push(e)});
    return {r,commits,errors,setRows:v=>rows=v,setIdentity:v=>identity=v,setVisible:v=>visible=v,setLoad:v=>loader=v};}
test('publishes initial list and changes only',async()=>{const s=setup();await s.r.refresh();await s.r.refresh();assert.equal(s.commits.length,1);s.setRows([{id:'b'}]);await s.r.refresh();assert.equal(s.commits.length,2);});
test('deletions publish empty list',async()=>{const s=setup();await s.r.refresh();s.setRows([]);await s.r.refresh();assert.deepEqual(s.commits.at(-1),[]);});
test('same catalog refreshes after identity change',async()=>{const s=setup();await s.r.refresh();s.setIdentity('2');await s.r.refresh();assert.equal(s.commits.length,2);});
test('skips hidden or logged-out page',async()=>{const s=setup();s.setVisible(false);await s.r.refresh();s.setVisible(true);s.setIdentity('');await s.r.refresh();assert.equal(s.commits.length,0);});
test('no overlapping requests',async()=>{const s=setup();let resolve;let calls=0;s.setLoad(()=>{calls++;return new Promise(r=>resolve=r)});const p=s.r.refresh();await s.r.refresh();assert.equal(calls,1);resolve([{id:'a'}]);await p;assert.equal(s.commits.length,1);});
test('ignores stale account results',async()=>{const s=setup();let resolve;s.setLoad(()=>new Promise(r=>resolve=r));const p=s.r.refresh();s.setIdentity('2');resolve([{id:'a'}]);await p;assert.equal(s.commits.length,0);});
test('keeps catalog on errors and reports once until recovery',async()=>{const s=setup();await s.r.refresh();s.setLoad(()=>Promise.reject(Error('offline')));await s.r.refresh();await s.r.refresh();assert.equal(s.commits.length,1);assert.equal(s.errors.length,1);s.setLoad(null);await s.r.refresh();s.setLoad(()=>Promise.reject(Error('offline again')));await s.r.refresh();assert.equal(s.errors.length,2);});
test('disposal ignores pending results',async()=>{const s=setup();let resolve;s.setLoad(()=>new Promise(r=>resolve=r));const p=s.r.refresh();s.r.dispose();resolve([{id:'a'}]);await p;await s.r.refresh();assert.equal(s.commits.length,0);});
