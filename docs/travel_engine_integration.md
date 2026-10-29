# Travel Engine in Pentacle

Pentacle can show the standalone Travel Engine through its Dashboards view. It embeds the original Travel Engine page, so Search, Wallet, Vault, filters, sharing, and booking controls continue to use the separately operated service and its same-origin APIs.

## Setup

1. Install and start the Travel Engine service separately. Its default local address is `http://127.0.0.1:8777`; see the sibling Travel Engine repository's setup guide for its prerequisites.
2. Add the service origin and enable Dashboards in the private Pentacle config file outside the repository:

   ```js
   module.exports = {
     // Keep your existing settings and feature flags in this object.
     features: {
       // Keep existing feature keys; add this flag to enable Dashboards.
       dashboards: true,
     },
     travelEngine: { url: 'http://127.0.0.1:8777' },
   };
   ```

3. Restart Pentacle, open **Dashboards**, and choose **Travel Engine**.

Do not add machine-specific service URLs or credentials to the public repository. Pentacle does not install or start the Travel Engine service. If the URL is absent, the dashboard gives setup instructions; if the page does not signal readiness within 10 seconds, it reports that the service is unavailable and offers Retry.

## URL and browser requirements

`travelEngine.url` must be an absolute HTTP(S) origin, optionally ending in `/`. Credentials, application paths, query strings, and fragments are rejected. Browser builds reject a service URL with the same origin as Pentacle. A browser-hosted Pentacle page served over HTTPS also requires an HTTPS Travel Engine URL. The Electron `file://` app can use a local HTTP service.

The embedded page is sandboxed with scripts, forms, modals, and same-origin storage enabled. It does not receive popup or top-navigation permission. Share copy is delegated through the iframe's clipboard permission where the browser supports it. The embedded page signals readiness and requests external booking links with `postMessage`; Pentacle checks the sending frame, configured origin, payload, and HTTP(S) destination before opening the link.

## Coverage and limits

The UI and backend test suites use checked-in search fixtures and in-memory service responses. They verify the airport picker, the embedded Search/Wallet/Vault views, sharing, and the booking review screen without confirming a purchase. These checks do not establish that live providers are reachable, credentials work, or a live booking can be completed. The Travel Engine service must be configured and running for real searches and vault operations.
