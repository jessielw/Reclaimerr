/**
 * shuffleArray - Shuffles an array in place using the Fisher-Yates algorithm.
 * @param array The array to shuffle.
 * @returns The shuffled array.
 */
const shuffleArray = <T>(array: T[]): T[] => {
  let currentIndex = array.length,
    randomIndex: number;

  // while there remain elements to shuffle.
  while (currentIndex !== 0) {
    // pick a remaining element.
    randomIndex = Math.floor(Math.random() * currentIndex);
    currentIndex--;
    // and swap it with the current element using array destructuring.
    [array[currentIndex], array[randomIndex]] = [
      array[randomIndex],
      array[currentIndex],
    ];
  }

  return array;
};

export { shuffleArray };
