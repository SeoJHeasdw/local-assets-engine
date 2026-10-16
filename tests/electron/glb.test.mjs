import test from 'node:test';
import assert from 'node:assert/strict';

import {checkMesh, primitives} from '../../electron-app/shared/glb.mjs';

// One triangle, positions as float32, no indices.
function glb(extra = {}) {
  const bin = new Uint8Array(new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]).buffer);
  const doc = {
    scenes: [{nodes: [0]}], nodes: [{mesh: 0, ...extra.node}],
    meshes: [{primitives: [{attributes: {POSITION: 0}, ...extra.primitive}]}],
    accessors: [{bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', ...extra.accessor}],
    bufferViews: [{buffer: 0, byteOffset: 0, byteLength: bin.byteLength}],
  };
  return {doc, bin};
}

test('the light mesh check refuses what decoding refuses, with the same sentence', () => {
  assert.doesNotThrow(() => checkMesh(glb()));
  assert.equal(primitives(glb()).length, 1);
  for (const extra of [{node: {skin: 0}}, {primitive: {extensions: {KHR_draco_mesh_compression: {}}}},
                       {accessor: {sparse: {}}}, {accessor: {componentType: 5122}}]) {
    let expected;
    try { primitives(glb(extra)); } catch (error) { expected = error.message; }
    assert.ok(expected, JSON.stringify(extra));
    assert.throws(() => checkMesh(glb(extra)), {message: expected});
  }
});
