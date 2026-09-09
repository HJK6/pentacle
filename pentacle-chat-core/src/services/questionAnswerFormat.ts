import type { PentacleQuestion, PentacleQuestionItem, PentacleQuestionOption } from '../types/pentacle';

export interface PentacleQuestionAnswerValue {
  selectedOptionLabel?: string;
  selectedOptionLabels?: readonly string[];
  selectedOptionIndex?: number;
  selectedOptionIndices?: readonly number[];
  text?: string;
  note?: string;
}

export interface BuildPentacleQuestionAnswerTextInput {
  question: PentacleQuestion;
  answers: readonly PentacleQuestionAnswerValue[];
}

type NormalizedQuestionItem = PentacleQuestionItem & {
  multiSelect?: boolean;
};

function questionItems(question: PentacleQuestion): NormalizedQuestionItem[] {
  if (Array.isArray(question.questions) && question.questions.length > 0) {
    return question.questions.map((item) => ({
      ...item,
      multiSelect: item.multiSelect ?? (question.questions?.length === 1 ? question.multiSelect : undefined),
    }));
  }
  return [{
    index: 0,
    header: question.header,
    prompt: question.prompt,
    options: question.options,
    multiSelect: question.multiSelect,
    selected_index: question.selected_index,
  }];
}

function normalizeLabel(label: string): string {
  return label.replace(/\r\n?/g, '\n').replace(/\n+/g, ' ');
}

function fallbackHeader(prompt: string): string {
  return prompt.trim().split(/\s+/).filter(Boolean).slice(0, 6).join(' ') || 'Question';
}

function displayHeader(item: NormalizedQuestionItem): string {
  const header = String(item.header || '').trim();
  return header || fallbackHeader(String(item.prompt || ''));
}

function optionLabelByIndex(options: readonly PentacleQuestionOption[], index: number): string {
  const option = options.find((candidate) => Number(candidate.index) === index);
  if (!option) {
    throw new Error(`question answer references unknown option index ${index}`);
  }
  return String(option.label || '');
}

function resolveSelectedLabels(
  item: NormalizedQuestionItem,
  answer: PentacleQuestionAnswerValue,
): string[] {
  const directLabels = [
    ...(answer.selectedOptionLabel === undefined ? [] : [answer.selectedOptionLabel]),
    ...(answer.selectedOptionLabels || []),
  ];
  const indexedLabels = [
    ...(answer.selectedOptionIndex === undefined ? [] : [answer.selectedOptionIndex]),
    ...(answer.selectedOptionIndices || []),
  ].map((index) => optionLabelByIndex(item.options || [], Number(index)));
  return [...directLabels, ...indexedLabels].map((label) => normalizeLabel(String(label)));
}

function buildQuestionLine(item: NormalizedQuestionItem, answer: PentacleQuestionAnswerValue, displayIndex: number): string {
  const header = displayHeader(item);
  const labels = resolveSelectedLabels(item, answer);
  const hasText = answer.text !== undefined;
  if (labels.length > 0 && hasText) {
    throw new Error('question answer may contain selected option labels or free text, not both');
  }
  if (item.multiSelect) {
    if (labels.length === 0 && !hasText) {
      return `Q${displayIndex} (${header}):`;
    }
    if (hasText) {
      return `Q${displayIndex} (${header}):\n${String(answer.text)}`;
    }
    return `Q${displayIndex} (${header}):\n${labels.map((label) => `- ${label}`).join('\n')}`;
  }
  if (hasText) {
    return `Q${displayIndex} (${header}):\n${String(answer.text)}`;
  }
  if (labels.length > 1) {
    throw new Error('single-select question answer may contain only one selected option label');
  }
  if (labels.length === 1) {
    return `Q${displayIndex} (${header}): ${labels[0]}`;
  }
  return `Q${displayIndex} (${header}):`;
}

/**
 * A single parsed question/answer block, recovered from the on-wire answer text
 * produced by {@link buildPentacleQuestionAnswerText}. This is a human-display
 * model: it carries the header label, the chosen answer (selected option
 * label(s) OR free text), and the optional note — enough to render a clean
 * structured bubble.
 */
export interface PentacleQuestionAnswerDisplayItem {
  /** The header/prompt label shown inside the parentheses of `Q<n> (header):`. */
  header: string;
  /** Selected option label(s); empty/absent when the answer is free text or no answer was given. */
  selectedLabels?: readonly string[];
  /** Free-text answer body; absent when the answer is option label(s) or no answer was given. */
  text?: string;
  /** Optional note line (`note (Q<n>): …`); absent when no note was attached. */
  note?: string;
}

/**
 * The structured display model for an answer message. `multiple` mirrors the
 * builder's plural intro (`Answering your questions:`).
 */
export interface PentacleQuestionAnswerDisplay {
  multiple: boolean;
  items: readonly PentacleQuestionAnswerDisplayItem[];
}

// Matches the leading sentinel line(s) the builder emits, plus the blank line
// separating the intro from the first block. `questions` (plural) distinguishes
// a multi-question message; capture group 1 is `question` | `questions`.
const ANSWER_INTRO_RE = /^Answering your (question|questions):\n\n/;
// A question block always opens with `Q<n> (header):`. Non-greedy header so the
// first `):` terminates it (matches the builder, which never emits `):` mid-header).
const QUESTION_HEADER_RE = /^Q(\d+) \(([\s\S]*?)\):/;

/**
 * Inverse of {@link buildPentacleQuestionAnswerText}: given the on-wire answer
 * text, recover the structured display model. Detection is by the
 * `Answering your question(s):` sentinel — the PRIMARY, uniform way to tell an
 * answer row from an ordinary user message.
 *
 * Returns `null` for any input that is NOT an answer message (no sentinel) or
 * is malformed, so the caller can fall back to a plain text bubble. NEVER
 * throws.
 */
export function parsePentacleQuestionAnswerText(text: string): PentacleQuestionAnswerDisplay | null {
  if (typeof text !== 'string') {
    return null;
  }
  try {
    const introMatch = ANSWER_INTRO_RE.exec(text);
    if (!introMatch) {
      return null;
    }
    const multiple = introMatch[1] === 'questions';
    const body = text.slice(introMatch[0].length);
    // Blocks are joined by a blank line; a new block always begins with the
    // `Q<n> (` token, so split only at blank lines that precede one. This keeps
    // free-text answers containing blank lines inside their own block.
    const rawBlocks = body.split(/\n\n(?=Q\d+ \()/);
    const items: PentacleQuestionAnswerDisplayItem[] = [];
    for (const block of rawBlocks) {
      const headerMatch = QUESTION_HEADER_RE.exec(block);
      if (!headerMatch) {
        // A block that does not open with the question grammar means the text
        // is not (or is no longer) a well-formed answer message.
        return null;
      }
      const questionIndex = headerMatch[1];
      const header = headerMatch[2];
      let remainder = block.slice(headerMatch[0].length);

      // Split off the trailing note, if any. The builder appends exactly one
      // `\nnote (Q<n>): <note>` at the very end of the block; take the last
      // occurrence so a note is never mistaken for free-text content.
      let note: string | undefined;
      const noteMarker = `\nnote (Q${questionIndex}): `;
      const noteAt = remainder.lastIndexOf(noteMarker);
      if (noteAt !== -1) {
        note = remainder.slice(noteAt + noteMarker.length);
        remainder = remainder.slice(0, noteAt);
      }

      const item: PentacleQuestionAnswerDisplayItem = { header };
      if (note !== undefined) {
        item.note = note;
      }

      if (remainder === '') {
        // Bare `Q<n> (header):` — no answer given (single-select empty,
        // multiSelect empty, or note-only).
      } else if (remainder.startsWith('\n')) {
        // Multi-line answer: either multiSelect bullets or free text.
        const content = remainder.slice(1);
        const lines = content.split('\n');
        const allBullets = lines.length > 0 && lines.every((line) => line.startsWith('- '));
        if (allBullets) {
          item.selectedLabels = lines.map((line) => line.slice(2));
        } else {
          item.text = content;
        }
      } else if (remainder.startsWith(' ')) {
        // Inline single-select label: `Q<n> (header): <label>`.
        item.selectedLabels = [remainder.slice(1)];
      } else {
        // Anything else is not the grammar the builder emits.
        return null;
      }

      items.push(item);
    }

    if (items.length === 0) {
      return null;
    }
    return { multiple, items };
  } catch {
    return null;
  }
}

export function buildPentacleQuestionAnswerText({
  question,
  answers,
}: BuildPentacleQuestionAnswerTextInput): string {
  const items = questionItems(question);
  const intro = items.length === 1 ? 'Answering your question:' : 'Answering your questions:';
  const blocks = items.map((item, position) => {
    const displayIndex = position + 1;
    const answer = answers[position] || {};
    const note = answer.note === undefined ? '' : `\nnote (Q${displayIndex}): ${String(answer.note)}`;
    return `${buildQuestionLine(item, answer, displayIndex)}${note}`;
  });
  return `${intro}\n\n${blocks.join('\n\n')}`;
}
