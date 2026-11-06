// Only the owning application receives its one-time authorization callback.
if (window.opener) {
  window.opener.postMessage(window.location.href, window.location.origin);
  window.close();
} else if (window.parent !== window) {
  // A silent callback targets only the owning application's same-origin parent,
  // never the cross-origin platform shell, and carries no bearer token.
  window.parent.postMessage(window.location.href, window.location.origin);
}
