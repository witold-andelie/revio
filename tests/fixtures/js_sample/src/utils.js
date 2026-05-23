// Two structurally identical functions — dedup candidates
function formatUserName(first, last) {
  const result = `${first} ${last}`;
  return result.trim();
}

function buildDisplayName(firstName, lastName) {
  const display = `${firstName} ${lastName}`;
  return display.trim();
}

// Single-call wrapper around an external lib
function wrapFetch(url) {
  return fetch(url);
}

// Never called
function deadHelper() {
  return Math.random();
}

module.exports = { formatUserName, buildDisplayName, wrapFetch };
