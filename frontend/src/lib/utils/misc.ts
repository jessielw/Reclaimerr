/**
 * Utility function to smoothly scroll to an element when a link is clicked
 * @param e The mouse event triggered by clicking the link
 * @returns void
 */
const scrollIntoView = (e: MouseEvent, highlight: boolean = false) => {
  e.preventDefault(); // prevent default anchor behavior
  const el = document.querySelector(
    (e.currentTarget as HTMLAnchorElement).getAttribute("href") || "",
  );
  if (!el) return;
  el.scrollIntoView({
    behavior: "smooth",
  });
  if (highlight) {
    el.classList.add("border-3", "border-call-to-action");
    setTimeout(() => {
      el.classList.remove("border-3", "border-call-to-action");
    }, 2000);
  }
};

export { scrollIntoView };
