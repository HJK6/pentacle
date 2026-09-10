'use strict';

// Cleanup failures are independent. Preserve the wrappers and paths when the
// private server may still be alive so an operator can finish cleanup safely.
async function cleanupOwnedFixtures(actions) {
  const result = { errors: [], serverCleaned: !actions.serverClaimed };
  async function attempt(stage, action) {
    try { await action(); return true; }
    catch (error) { result.errors.push({ stage, message: error.message }); return false; }
  }
  await attempt('restoreClipboard', actions.restoreClipboard);
  await attempt('disposePtys', actions.disposePtys);
  if (actions.serverClaimed) {
    try {
      await actions.killServer();
      result.serverCleaned = true;
    } catch (error) {
      try { result.serverCleaned = await actions.verifyServerAbsent(); }
      catch (verificationError) {
        result.errors.push({ stage: 'verifyServerAbsent', message: verificationError.message });
      }
      if (!result.serverCleaned) result.errors.push({ stage: 'killServer', message: error.message });
    }
  }
  if (result.serverCleaned) {
    if (actions.removeRemote) result.remoteDirectoryCleaned = await attempt('removeRemote', actions.removeRemote);
    result.localDirectoryCleaned = await attempt('removeLocal', actions.removeLocal);
  } else {
    result.fixtureDirectoriesPreserved = true;
  }
  return result;
}

function isVerifiedAbsent(error) {
  // An SSH transport error, missing executable, or permission error is not
  // evidence that the owned server is gone.
  return error?.code === 1 && /^no server running on [^\r\n]+$/.test(String(error.stderr || '').trim());
}

module.exports = { cleanupOwnedFixtures, isVerifiedAbsent };
