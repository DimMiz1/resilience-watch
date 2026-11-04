"use strict";
function publicReviewURL(href) {
  let url;
  try { url = new URL(href); }
  catch { return null; }
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  // Require a hosted HTTPS domain, never a local preview or numeric IP address.
  if (url.protocol !== "https:" || !host.includes(".") || host.includes(":") ||
      /^[\d.]+$/.test(host) || /\.(localhost|local|internal|test)$/.test(host)) return null;
  return new URL("/review", url.origin).href;
}
function mentorEmailURL(href) {
  const url = publicReviewURL(href);
  if (!url) return null;
  const subject = "CASSINI team prototype: Resilience Watch";
  const body = `Hello,\n\nHere is a browser preview of our work in progress for CASSINI Challenge 2: Space-enabled crisis response and civil resilience.\n\n${url}\n\nNo account or installation is required. The introduction explains how to try the map and its limitations. This is a research prototype, not an official warning or safe-evacuation service; mapped facilities are unverified.\n\nWe would value your feedback on the concept, data sources, validation and how this can support the team's work on crisis-related indirect costs.\n\nBest regards`;
  return `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
}
const publicLink = publicReviewURL(window.location.href);
const shareField = document.getElementById("share-url");
const shareStatus = document.getElementById("sharing-status");
const emailLink = document.getElementById("email-app");
const copyButton = document.getElementById("copy-app-link");
if (!publicLink) {
  shareField.value = "";
  copyButton.disabled = true;
  emailLink.setAttribute("aria-disabled", "true");
  emailLink.addEventListener("click", event => event.preventDefault());
  shareStatus.textContent = "This is a local or non-public preview, not a shareable link. Publish the app on a public HTTPS host first; localhost opens the recipient's own device, not this computer.";
} else {
  shareField.value = publicLink;
  emailLink.href = mentorEmailURL(publicLink);
  shareStatus.textContent = "Prepare email opens a draft in your email app; it does not send anything. You can also copy the link.";
}
copyButton.addEventListener("click", async () => {
  if (!publicLink) return;
  try {
    if (!navigator.clipboard) {
      shareField.focus();
      shareField.select();
      shareStatus.textContent = "Clipboard is unavailable. The link is selected; copy it manually.";
      return;
    }
    await navigator.clipboard.writeText(publicLink);
    shareStatus.textContent = "Mentor preview link copied. No GPS coordinates are included.";
  } catch (error) {
    shareField.focus();
    shareField.select();
    shareStatus.textContent = `Could not copy the link: ${error.message}. The link is selected for manual copying.`;
  }
});
