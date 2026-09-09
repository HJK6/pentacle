// Walk: report_comment_stability — comment mutations never blank the report
// viewer or move its scroll position (public-ui-regression).
//
// Reported rendering defect: every comment submit blacked out the report tab and
// reset scroll to the top, because comment mutations were routed through the
// teardown re-render (renderSlotAsset force path) instead of an in-place update.
//
// This walk drives the REAL renderer in real Electron (real layout, real
// scrollTop clamping — the parts JSDOM cannot exercise) over a fixture report
// tall enough to scroll:
//   1. add 3 comments mid-document through the real UI path (each followed by
//      the daemon-shaped comment-only asset.update broadcast),
//   2. edit one and delete one,
//   3. apply one EXTERNAL resolve via the push path (the viewer has no resolve
//      control by design — resolve is CLI/agent-side),
// asserting after each step: scrollTop stable within a few pixels, marker state
// updated, and NO `asset:rendered` beacon (negative window — the in-place path
// must not re-mount). A final republish (bumped updated_at) is the positive
// control: it MUST re-render fully (existing per-revision contract).
//
// Run:
//   PENTACLE_PROFILE= PUBLIC_WALK_NO_BUILD=1 PENTACLE_WALK_ALT=1 \
//     node test/e2e/cli.js report_comment_stability \
//     --config test/e2e/configs/report_comment_stability.local.js
// PENTACLE_PROFILE= clears a shared-profile env (the walk daemon runs a
// single fixture host); PUBLIC_WALK_NO_BUILD=1 trusts the committed renderer
// bundle (the walk exercises unbundled renderer files only).
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude', 'codex'] };

const FIXTURE = {
  slot: 0,
  hostId: 'local',
  host: 'hostb',
  sessionName: 'codex-report-stability-e2e',
  displayName: 'Report Stability Fixture',
  streamId: 'hostb:codex-report-stability-e2e',
  assetId: 'report-stability-fixture-1',
  title: 'Report Comment Stability',
  updatedAt: '2026-07-09T12:00:00Z',
  republishedAt: '2026-07-09T12:30:00Z',
};

const SCROLL_TOLERANCE_PX = 4;

function reportBody(revision) {
  const sections = [];
  for (let i = 1; i <= 24; i++) {
    sections.push({
      id: `sec-${i}`,
      title: `Section ${i}`,
      status: 'in_progress',
      blocks: [{
        id: `block-${i}`,
        type: 'para',
        runs: [{ type: 'text', text: `${revision} paragraph ${i} — enough text to give the section body height in the viewer.` }],
      }],
    });
  }
  return JSON.stringify({ schema_version: 1, title: `Stability ${revision}`, sections });
}

async function run(ctx) {
  const setup = await ctx.eval(`(async () => {
    const fixture = ${JSON.stringify(FIXTURE)};
    state.chatStream.sessions = [{
      host: fixture.host,
      session_name: fixture.sessionName,
      stream_id: fixture.streamId,
      provider: 'codex',
      status: 'active',
    }];
    window.cc.createPty = async () => 'report-stability-fixture-pane';
    window.cc.resizePty = () => {};
    const e2e = window.__reportStabilityE2e = {
      body: ${JSON.stringify(reportBody('First'))},
      updatedAt: fixture.updatedAt,
      comments: [],
      calls: { assetGet: 0, list: 0 },
      nextId: 1,
    };
    const originalAssetGet = window.cc.assetGet;
    window.cc.assetGet = async (args) => {
      if (args && args.asset_id === fixture.assetId) {
        e2e.calls.assetGet += 1;
        return {
          ok: true,
          asset: {
            asset_id: fixture.assetId,
            title: fixture.title,
            content_type: 'report',
            review_status: 'pending_review',
            body: e2e.body,
            updated_at: e2e.updatedAt,
          },
        };
      }
      return originalAssetGet ? originalAssetGet(args) : { ok: false };
    };
    window.cc.assetCommentsList = async () => {
      e2e.calls.list += 1;
      return { ok: true, comments: e2e.comments.slice() };
    };
    window.cc.assetCommentAdd = async (args) => {
      const comment = {
        comment_id: 'c-' + (e2e.nextId++),
        section_id: args.section_id,
        block_id: args.block_id,
        excerpt: args.excerpt || '',
        body: args.body,
        author: 'operator@e2e',
        created_at: new Date().toISOString(),
        resolved: false,
      };
      e2e.comments.push(comment);
      return { ok: true, comment };
    };
    window.cc.assetCommentEdit = async (args) => {
      const comment = e2e.comments.find((c) => c.comment_id === args.comment_id);
      if (comment) comment.body = args.body;
      return { ok: true, comment };
    };
    window.cc.assetCommentDelete = async (args) => {
      const index = e2e.comments.findIndex((c) => c.comment_id === args.comment_id);
      const removed = index >= 0 ? e2e.comments.splice(index, 1)[0] : null;
      return { ok: true, comment: removed };
    };
    window.__reportStabilityPush = () => applyChatStreamPayload({
      type: 'asset.update',
      session_key: { host: fixture.host, session_name: fixture.sessionName, stream_id: fixture.streamId },
      asset_id: fixture.assetId,
      title: fixture.title,
      content_type: 'report',
      review_status: 'pending_review',
      tags: ['e2e'],
      updated_at: e2e.updatedAt,
    });
    await attachSession(fixture.slot, fixture.sessionName, fixture.displayName, fixture.hostId);
    updateSlotViewMode(fixture.slot, 'chat');
    window.__reportStabilityPush();
    return { tab: !!document.querySelector('#header-0 .slot-asset-tab') };
  })()`);
  ctx.assert('fixture report asset tab rendered', setup.tab, setup);

  let seq = ctx.beaconSeq();
  ctx.assert('asset tab clicked', await ctx.click('#header-0 .slot-asset-tab'));
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'asset:rendered' && b.slot === 0, { label: 'report rendered' });
  await ctx.waitFor(
    `document.querySelector('#cell-0 .slot-asset-layer')?.textContent.includes('First paragraph 12')`,
    { label: 'report body visible' },
  );

  // Scroll mid-document in real layout; the mount must actually be scrollable.
  const scrolled = await ctx.eval(`(() => {
    const mount = document.querySelector('#cell-0 .slot-asset-layer');
    mount.scrollTop = Math.floor(mount.scrollHeight / 2);
    return { scrollTop: mount.scrollTop, scrollHeight: mount.scrollHeight, clientHeight: mount.clientHeight };
  })()`);
  ctx.assert('report is scrollable in real layout', scrolled.scrollHeight > scrolled.clientHeight + 200, scrolled);
  ctx.assert('scrolled mid-document', scrolled.scrollTop > 100, scrolled);
  await ctx.screenshot('report-scrolled');

  // A comment step = the full real flow: select block, compose, submit, then the
  // daemon-shaped comment-only broadcast. Returns scroll + marker + node identity.
  const step = async (label, js) => {
    const fromSeq = ctx.beaconSeq();
    const result = await ctx.eval(`(async () => {
      const mount = document.querySelector('#cell-0 .slot-asset-layer');
      const before = mount.scrollTop;
      const rootBefore = mount.querySelector('.slot-asset-report');
      ${js}
      await new Promise((resolve) => setTimeout(resolve, 150));
      window.__reportStabilityPush();
      await new Promise((resolve) => setTimeout(resolve, 150));
      const after = document.querySelector('#cell-0 .slot-asset-layer');
      return {
        scrollBefore: before,
        scrollAfter: after.scrollTop,
        sameRoot: after.querySelector('.slot-asset-report') === rootBefore,
        comments: window.__reportStabilityE2e.comments.length,
        markers: Array.from(after.querySelectorAll('.slot-asset-report-comment-pin'))
          .filter((pin) => pin.textContent !== '+').length,
      };
    })()`);
    ctx.assert(`${label}: scroll stable`, Math.abs(result.scrollAfter - result.scrollBefore) <= SCROLL_TOLERANCE_PX, result);
    ctx.assert(`${label}: report root not re-mounted`, result.sameRoot, result);
    await ctx.assertNoBeacon(
      (b) => b.seq > fromSeq && b.name === 'asset:rendered' && b.slot === 0,
      { windowMs: 700, fromSeq, label: `${label}: asset re-rendered (teardown path)` },
    );
    return result;
  };

  const addJs = (blockId, text) => `
      mount.querySelector('[data-block-id="${blockId}"] .slot-asset-report-block-body')
        .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      const input = mount.querySelector('.slot-asset-report-comment-input');
      input.value = ${JSON.stringify(text)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      mount.querySelector('.slot-asset-report-comment-submit').click();
  `;

  const add1 = await step('add comment 1', addJs('block-10', 'First pass note'));
  ctx.assert('comment 1 marker appears', add1.markers === 1 && add1.comments === 1, add1);
  const add2 = await step('add comment 2', addJs('block-12', 'Second pass note'));
  ctx.assert('comment 2 marker appears', add2.markers === 2 && add2.comments === 2, add2);
  const add3 = await step('add comment 3', addJs('block-14', 'Third pass note'));
  ctx.assert('comment 3 marker appears', add3.markers === 3 && add3.comments === 3, add3);
  await ctx.screenshot('report-three-comments');

  const edited = await step('edit comment', `
      mount.querySelector('[data-block-id="block-10"] .slot-asset-report-comment-pin')
        .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      mount.querySelector('.slot-asset-report-comment-actions [title="Edit"]').click();
      const input = mount.querySelector('.slot-asset-report-comment-input');
      input.value = 'First pass note (edited)';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      mount.querySelector('.slot-asset-report-comment-submit').click();
  `);
  ctx.assert('edit kept all three comments', edited.comments === 3, edited);

  const deleted = await step('delete comment', `
      mount.querySelector('[data-block-id="block-12"] .slot-asset-report-comment-pin')
        .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      mount.querySelector('.slot-asset-report-comment-actions [title="Delete"]').click();
  `);
  ctx.assert('delete removed one comment', deleted.comments === 2 && deleted.markers === 2, deleted);

  // External resolve (CLI/agent-side): flip fixture state, then only the push.
  const resolved = await step('external resolve via push', `
      const target = window.__reportStabilityE2e.comments.find((c) => c.block_id === 'block-10');
      target.resolved = true;
  `);
  const unresolvedGone = await ctx.eval(
    `!document.querySelector('#cell-0 .slot-asset-layer [data-block-id="block-10"]').classList.contains('has-unresolved-comments')`,
  );
  ctx.assert('external resolve reflected in place', resolved.sameRoot && unresolvedGone, resolved);
  await ctx.screenshot('report-after-mutations');

  // Positive control: a republish (bumped updated_at) still swaps the document
  // through the full re-render path.
  seq = ctx.beaconSeq();
  await ctx.eval(`(() => {
    const e2e = window.__reportStabilityE2e;
    e2e.body = ${JSON.stringify(reportBody('Second'))};
    e2e.updatedAt = ${JSON.stringify(FIXTURE.republishedAt)};
    window.__reportStabilityPush();
    return true;
  })()`);
  await ctx.awaitBeacon((b) => b.seq > seq && b.name === 'asset:rendered' && b.slot === 0, { label: 'republish re-renders' });
  await ctx.waitFor(
    `document.querySelector('#cell-0 .slot-asset-layer')?.textContent.includes('Second paragraph 12')`,
    { label: 'republished body visible' },
  );
  await ctx.screenshot('report-republished');
}

module.exports = { SCENARIO_META, run };
