/** @type {Record<string, string[]>} */
const PROVIDER_ALIASES = {
  seerr: ["overseerr", "jellyseerr", "request", "requests"],
  rottentomatoes: [
    "rotten tomatoes",
    "rt",
    "tomatometer",
    "popcornmeter",
    "critic score",
    "audience score",
  ],
  tmdb: ["the movie database"],
  imdb: ["internet movie database"],
  tvdb: ["thetvdb", "the tv database"],
};

/** @type {Record<string, string[]>} */
const FIELD_ALIASES = {
  "playback.fully_watched_usernames": [
    "finished",
    "completed",
    "watched by user",
    "fully watched by user",
  ],
  "series.fully_watched": [
    "series finished",
    "show fully watched",
    "completed",
  ],
  "series.watched_percent": ["series progress", "show watched percent"],
  "series.status": ["tv status", "show status", "television status"],
  "sonarr.series_status": ["sonarr status", "tv status", "show status"],
  "rottentomatoes.tomato_meter": ["tomato score", "critic score"],
  "rottentomatoes.popcorn_meter": ["audience score", "audience rating"],
};

/**
 * Convert labels, field keys, and queries into comparable search words. Field
 * keys use dots and underscores, while provider names such as
 * `rottentomatoes` need their aliases to be useful as normal words.
 *
 * @param {string} value
 * @returns {string[]}
 */
function searchWords(value) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
}

/**
 * Give a field searchable synonyms in addition to the label rendered in the
 * picker. This keeps provider context when a field appears under Common.
 *
 * @param {string} value
 * @param {string} label
 * @param {string} category
 * @returns {string[]}
 */
export function ruleFieldSearchKeywords(value, label, category) {
  const provider = value.split(".")[0];
  return [
    label,
    category,
    value,
    ...(PROVIDER_ALIASES[provider] ?? []),
    ...(FIELD_ALIASES[value] ?? []),
  ];
}

/**
 * Return the Levenshtein distance between two short search words.
 *
 * @param {string} left
 * @param {string} right
 * @returns {number}
 */
function editDistance(left, right) {
  let previous = Array.from({ length: right.length + 1 }, (_, index) => index);
  for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
    const current = [leftIndex];
    for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
      current[rightIndex] = Math.min(
        current[rightIndex - 1] + 1,
        previous[rightIndex] + 1,
        previous[rightIndex - 1] +
          (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1),
      );
    }
    previous = current;
  }
  return previous[right.length];
}

/**
 * Score one query word against a field-search word.
 *
 * @param {string} queryWord
 * @param {string} candidateWord
 * @returns {number}
 */
function wordScore(queryWord, candidateWord) {
  if (queryWord === candidateWord) return 1;
  if (queryWord.length >= 3 && candidateWord.startsWith(queryWord)) return 0.94;
  if (queryWord.length >= 3 && candidateWord.includes(queryWord)) return 0.88;
  if (candidateWord.length >= 3 && queryWord.includes(candidateWord))
    return 0.8;

  const maxDistance = queryWord.length >= 7 ? 2 : queryWord.length >= 4 ? 1 : 0;
  if (maxDistance === 0) return 0;

  const distance = editDistance(queryWord, candidateWord);
  return distance <= maxDistance ? 0.72 - (distance - 1) * 0.12 : 0;
}

/**
 * Token-aware Command filter for rule fields. Every query word is matched
 * independently, so users can search naturally ("series status") and minor
 * spelling mistakes ("rotton tomatoes") still surface the intended fields.
 *
 * Partial multi-word matches deliberately remain visible at a lower rank:
 * there is no single "Seerr series status" field, but a user should still see
 * the relevant series-status choices instead of an empty picker.
 *
 * @param {string} value
 * @param {string} query
 * @param {string[]} [keywords]
 * @returns {number}
 */
export function scoreRuleFieldSearch(value, query, keywords = []) {
  const queryWords = searchWords(query);
  if (queryWords.length === 0) return 1;

  const candidateWords = searchWords([value, ...keywords].join(" "));
  if (candidateWords.length === 0) return 0;

  const scores = queryWords.map((queryWord) =>
    Math.max(
      ...candidateWords.map((candidateWord) =>
        wordScore(queryWord, candidateWord),
      ),
    ),
  );
  const matches = scores.filter((score) => score > 0);
  if (matches.length === 0) return 0;

  const coverage = matches.length / queryWords.length;
  const averageScore =
    matches.reduce((total, score) => total + score, 0) / matches.length;
  if (coverage === 1) return 0.65 + averageScore * 0.35;

  // Do not flood a one-word search with weak approximate matches. For a
  // multi-word query, retain strong partial matches so adjacent concepts stay
  // discoverable rather than disappearing altogether.
  if (queryWords.length === 1 || coverage < 0.5) return 0;
  return 0.2 + coverage * 0.4 + averageScore * 0.2;
}
