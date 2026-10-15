// SPDX-License-Identifier: MIT
const { test } = require('node:test'); const assert = require('node:assert/strict'); const path = require('node:path');
const build = process.env.SAM2_TEST_BUILD;
const { track } = require(path.join(build, 'tracking.js'));
const polygon = { type: 'polygon', points: [1, 1, 9, 1, 9, 9, 1, 9] };
const meta = { width: 20, height: 20, deleted: false };
test('reverse tracks descending images and closes both bounds with the same object order', async () => {
    const calls = [];
    const result = await track({ seeds: [polygon, polygon], start: 4, stop: 6, first: 0, direction: -1, span: 2,
        frameNumbers: [0,1,2,3,4,5,6], width: 20, height: 20 }, {
        frame: async () => meta, call: async (args) => { calls.push(args); return { states:['a','b'], shapes:[polygon,null] }; },
    }, () => false, () => {});
    assert.deepEqual(calls.map((call) => call.frame), [4,3,2]);
    assert.deepEqual(result[0].map((shape) => [shape.frame, shape.outside]), [[1,true],[2,false],[3,false],[4,false],[5,true]]);
    assert.deepEqual(result[1].map((shape) => [shape.frame, shape.outside]), [[1,true],[2,true],[3,true],[4,false],[5,true]]);
});
test('mask initialization uses full RLE and returned polygon for its first keyframe', async () => {
    const seed = { type:'mask', points:[0,9,1,1,3,3] }; const calls=[];
    const result=await track({ seeds:[seed],start:0,stop:1,span:1,frameNumbers:[0,1],width:20,height:20 }, {
        frame:async()=>meta,call:async(args)=>{calls.push(args);return { states:['a'], shapes:[polygon] };},
    },()=>false,()=>{});
    assert.deepEqual(calls[0].shapes,[seed]);assert.deepEqual(result[0][0].points,polygon.points);
});
