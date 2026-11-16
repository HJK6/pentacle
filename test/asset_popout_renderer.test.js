const test = require('node:test');
const assert = require('node:assert/strict');

const { sameAssetUpdateForItem } = require('../renderer/asset_popout');

test('asset pop-out update filter keeps spec-scoped assets isolated', () => {
  const item = {
    asset_id: 'shared-id',
    spec_id: 'fixture-a',
    session_key: {
      host: 'hostb',
      session_name: 'provider_c-1',
      stream_id: 'hostb:provider_c-1',
    },
  };

  assert.equal(sameAssetUpdateForItem(item, 'hostb:provider_c-1', {
    type: 'asset.update',
    asset_id: 'shared-id',
    spec_id: 'fixture-a',
    session_key: { stream_id: 'other:stream' },
  }), true);
  assert.equal(sameAssetUpdateForItem(item, 'hostb:provider_c-1', {
    type: 'asset.update',
    asset_id: 'shared-id',
    session_key: { stream_id: 'hostb:provider_c-1' },
  }), false);
  assert.equal(sameAssetUpdateForItem({ ...item, spec_id: null }, 'hostb:provider_c-1', {
    type: 'asset.update',
    asset_id: 'shared-id',
    spec_id: 'fixture-a',
    session_key: { stream_id: 'hostb:provider_c-1' },
  }), false);
});
