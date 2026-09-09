'use strict';

function validStateVersion(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function actionableConnectionError(value) {
  const error = String(value || '').trim();
  return error && error !== 'Stream disconnected';
}

function applyVersionedConnectionState(chatStream, payload, setDegradedMode) {
  if (!chatStream || !payload) return false;
  const incomingVersion = payload.state_version;
  const currentVersion = chatStream.stateVersion;
  const sameVersion = validStateVersion(incomingVersion)
    && validStateVersion(currentVersion)
    && incomingVersion === currentVersion;

  if (validStateVersion(incomingVersion)) {
    if (validStateVersion(currentVersion) && incomingVersion < currentVersion) return false;
    chatStream.stateVersion = incomingVersion;
  } else if (validStateVersion(currentVersion)) {
    // Once the versioned protocol is active, an unversioned payload cannot
    // establish whether it predates the current connection state.
    return false;
  }

  chatStream.connected = !!payload.connected;
  if (chatStream.connected) {
    chatStream.error = null;
  } else if (Object.prototype.hasOwnProperty.call(payload, 'error')) {
    const nextError = payload.error ? String(payload.error) : null;
    // A startup get-state pull can have been captured before a pre-socket
    // refusal was published. Both payloads may carry the same connection
    // version because connectivity stayed false; retain the actionable reason
    // instead of letting a generic stale disconnect erase the banner.
    if (!(sameVersion && actionableConnectionError(chatStream.error)
      && !actionableConnectionError(nextError))) {
      chatStream.error = nextError;
    }
  }
  setDegradedMode(!chatStream.connected);
  return true;
}

module.exports = { applyVersionedConnectionState };
