/** These cards are also used by the local Korean audio checks. */
import cards from './character-cards.json' with { type: 'json' };

export const CHARACTER_CARDS = Object.freeze({
  ko: Object.freeze(cards.ko),
  ja: Object.freeze(cards.ja),
});

export function sessionPrompt(language) {
  const card = CHARACTER_CARDS[language];
  return card === null ? { system_prompt: '' } : { card };
}
