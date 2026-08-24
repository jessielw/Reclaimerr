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
 * @returns {{ text: string, warning: boolean }}
 */
export function movieRequesterWatchSummary(requester, requestDateGateIgnored) {
  if (requester.movie_watched_at === null) {
    return {
      text: "No completed movie watch found for this requester.",
      warning: true,
    };
  }
  if (requester.movie_watched_before_request) {
    if (requestDateGateIgnored) {
      return {
        text: "Watched the movie before requesting it — still counted.",
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
