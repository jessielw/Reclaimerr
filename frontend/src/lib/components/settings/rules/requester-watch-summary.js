/**
 * @typedef {object} MovieRequesterWatchDetail
 * @property {string | null} movie_watched_at
 * @property {boolean} movie_watched_before_request
 */

/**
 * Describe one movie requester's completed-watch state without borrowing the
 * aggregate result, which may have been satisfied by a different requester.
 *
 * @param {MovieRequesterWatchDetail} requester
 * @param {boolean} requestDateGateIgnored
 * @param {boolean} [gatedHalfUsed] whether the rule asks the request-date-gated
 *   question at all. A watch that predates the request is no caveat when it
 *   does not, so say so rather than warning about it.
 * @returns {{ text: string, warning: boolean }}
 */
export function movieRequesterWatchSummary(
  requester,
  requestDateGateIgnored,
  gatedHalfUsed = true,
) {
  if (requester.movie_watched_at === null) {
    return {
      text: "No completed movie watch found for this requester.",
      warning: true,
    };
  }
  if (requester.movie_watched_before_request) {
    if (requestDateGateIgnored) {
      return {
        text: "Watched the movie before requesting it - still counted.",
        warning: false,
      };
    }
    if (!gatedHalfUsed) {
      return {
        text: "Watched the movie before requesting it - not used by this rule.",
        warning: false,
      };
    }
    return {
      text: "Watched the movie, but only before requesting it.",
      warning: true,
    };
  }
  return {
    text: "Watched the movie, after requesting it.",
    warning: false,
  };
}
