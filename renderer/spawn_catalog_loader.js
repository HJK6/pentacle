'use strict';

function isUsableSpawnCatalogResponse(response) {
  return !!response?.ok && !!response.catalog?.profiles?.desktop_manual;
}

function createSpawnCatalogLoader(fetchCatalog) {
  let cachedCatalog = null;
  let inFlight = null;

  async function load() {
    if (cachedCatalog) return { ok: true, catalog: cachedCatalog };
    if (!inFlight) {
      inFlight = Promise.resolve()
        .then(() => fetchCatalog())
        .then((response) => {
          if (isUsableSpawnCatalogResponse(response)) cachedCatalog = response.catalog;
          return response;
        })
        .finally(() => {
          inFlight = null;
        });
    }
    return inFlight;
  }

  return {
    load,
    peek: () => cachedCatalog,
  };
}

module.exports = {
  createSpawnCatalogLoader,
  isUsableSpawnCatalogResponse,
};
